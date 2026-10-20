"""The accumulator that spills a captured stream to a temporary file.

This is the one policy whose storage is not the parent heap, so it is the only
one that owns a file and, with it, a cleanup obligation. It carries its own
module because that obligation — creating, appending to, and eventually
removing a file without ever letting a storage failure reach the child path —
is most of what the class does.

The invariant this module exists to hold: **a storage failure is absorbed**.
The accumulator reports it, emits a
:class:`~cuprum.capture_events.CaptureEvent`, and lets the run complete. The
child is still reaped and the caller still receives a result, because a failure
to *retain* output is not a failure of the command that produced it. What the
caller can still read differs by when the failure struck, and both choices
prefer keeping their data:

- A failure to *create* the spill file leaves the stream on the parent heap.
  Nothing was written anywhere else, so buffering continues in memory and the
  caller still reads complete text from ``stdout``/``stderr``; the record
  reports ``storage`` of ``"memory"`` beside ``storage_failed``.
- A failure to *append* after the spill stops accepting new writes and marks
  the capture failed, but bytes accepted before it still reach the file:
  finalization flushes them before a byte-mode read-back. Cleanup still removes
  the file, and ``storage_failed`` tells the caller the retained capture is
  incomplete rather than merely short.

Every filesystem call is made on a worker thread through
:mod:`cuprum._capture_spill_io`, and the calls for one accumulator are
serialized, so the file's contents are the order the child wrote rather than
the order the executor happened to run them in.
"""

from __future__ import annotations

import contextlib
import pathlib
import typing as typ

from cuprum._capture_decode import _DecodeSettings, _render
from cuprum._capture_spill_io import (
    _create_published_spill,
    _publish_spill,
    _SpillIo,
)
from cuprum._capture_types import CaptureFinal, SpillHandle
from cuprum.capture_events import CaptureEvent, CaptureEventCategory
from cuprum.capture_observation import _emit_capture_event
from cuprum.capture_policy import CapturedStream, FileBackedCapture, policy_kind

if typ.TYPE_CHECKING:
    from cuprum.capture_policy import CaptureStreamName

__all__ = [
    "_FileAccumulator",
]


class _FileAccumulator:
    """Keep the stream on the heap, then spill it to a temporary file.

    The parent holds the stream only until it passes ``spill_threshold_bytes``;
    past that point the bytes already buffered are written to a file and every
    later chunk is appended there, so retention stops tracking the child's
    output. A stream that never crosses the threshold finalizes from memory,
    exactly as :class:`~cuprum.capture_policy.FullCapture` would have finalized
    it, so raising the threshold is a memory decision and never changes what
    the caller reads.
    """

    __slots__ = (
        "_buffer",
        "_failed",
        "_file",
        "_flushed_bytes",
        "_handle",
        "_io",
        "_path",
        "_policy",
        "_provisional",
        "_settings",
        "_spilled_bytes",
        "_stream",
        "_total",
    )

    def __init__(
        self,
        *,
        policy: FileBackedCapture,
        stream: CaptureStreamName,
        settings: _DecodeSettings,
    ) -> None:
        """Start an empty capture of *stream* that spills once *policy* allows."""
        self._policy = policy
        self._stream = stream
        self._settings = settings
        # The ordering seam and its worker calls. One per accumulator, so two
        # streams spilling at once do not wait for each other, and one per
        # *file*, so this capture's own calls cannot be reordered.
        self._io = _SpillIo()
        self._buffer: bytearray | None = bytearray()
        # Whichever type `_create_spill` hands back — the temporary-file wrapper,
        # not the buffered file it forwards to; the two annotations must agree
        # or every assignment between them fails the typechecker.
        self._file: typ.IO[bytes] | None = None
        # One handle for the capture's life, not one per query: the finalized
        # result closes it while teardown may also close it from the sink, and
        # a fresh object each time would give those paths independent flags.
        self._handle: SpillHandle | None = None
        # The same handle while it is still provisional: published so teardown
        # can remove a half-made file, and not yet carrying the caller's keep
        # policy. Cleared by `_promote_provisional_spill` once the prefix has
        # landed.
        self._provisional: SpillHandle | None = None
        self._path: pathlib.Path | None = None
        self._total = 0
        self._spilled_bytes = 0
        # What has reached the file, as opposed to what has been handed to the
        # buffered writer. `_append` defers its flush, so the two diverge until
        # `_flush_before_reading`; a failed flush there reports this count
        # rather than the larger one the writes added up to.
        self._flushed_bytes = 0
        # The *name* of the class behind a storage failure, never the
        # exception: it is reported through an event channel whose consumers
        # treat fields as low-cardinality labels.
        self._failed: str | None = None

    async def absorb(self, chunk: bytes) -> None:
        """Buffer the chunk, spill it, or append it, by where the capture is."""
        self._total += len(chunk)
        if self._path is not None:
            await self._append(chunk)
            return
        buffered = self._buffer
        if buffered is None:
            return
        buffered.extend(chunk)
        if self._should_spill(len(buffered)):
            await self._spill()

    async def finalize(self) -> CaptureFinal:
        """Render spilled or in-memory bytes, with the record describing them."""
        # Everything this capture handed to a worker has landed by the time
        # each `await` above returned, so both branches read state no worker
        # can still be changing. `absorb` and `finalize` are awaited by the
        # same drain task in order, never concurrently.
        path = self._path
        if path is not None:
            return await self._finalize_spilled(path)
        return self._finalize_buffered()

    async def cleanup(self) -> None:
        """Close this run's spill file, removing it unless the caller kept it."""
        handle = self._handle
        if handle is not None:
            # ``handle.keep`` rather than the policy: a handle published
            # provisionally, before a spill finished, carries ``keep=False``
            # even under ``keep_file=True``. See :meth:`_spill`.
            await handle.close(remove=not handle.keep)

    def _should_spill(self, buffered: int) -> bool:
        """Whether *buffered* bytes have outgrown the configured threshold."""
        # A failed spill transition leaves ``_path`` unset, so keep the
        # accumulator on the heap instead of retrying a broken directory on
        # every chunk and emitting a storage-failure event each time. Once a
        # spill succeeds, ``_path`` is set and ``absorb`` takes the append
        # branch; later write, flush, or read-back failures never ask this
        # predicate again.
        if self._failed is not None:
            return False
        threshold = self._policy.spill_threshold_bytes
        return threshold is None or buffered > threshold

    async def _spill(self) -> None:
        """Move the buffered prefix into a new file and append there after.

        A failure here leaves the stream on the heap: nothing was written
        anywhere else, so buffering continues and the caller still reads
        complete text. The half-made file is removed and the sink stops
        retrying, so a full or read-only directory costs one syscall rather
        than one per chunk.

        The handle is published before the prefix is written, so a cancellation
        during those awaits still leaves teardown a way to remove what was
        created. The caller's keep policy is applied by
        :meth:`_promote_provisional_spill` once the spill has succeeded, since
        only a file that took the prefix has earned it.
        """
        buffered = self._buffer
        if buffered is None:
            return
        try:
            file = await _create_published_spill(
                self._io, self._policy.directory, self._adopt_spill
            )
        except OSError as exc:
            self._fail_storage(exc, CaptureEventCategory.STORAGE_WRITE_FAILED)
            return
        try:
            # ``bytes`` rather than the live buffer: the worker must see the
            # prefix as it stood when the spill was decided, not whatever a
            # later read appends before the worker is scheduled.
            await self._io.run(file.write, bytes(buffered))
            await self._io.run(file.flush)
        except OSError as exc:
            await self._discard_failed_spill(file, exc)
            return
        self._spilled_bytes = len(buffered)
        self._flushed_bytes = len(buffered)
        self._file = file
        path = pathlib.Path(file.name)
        self._path = path
        self._promote_provisional_spill()
        self._buffer = None
        _emit_capture_event(
            CaptureEvent(
                stream=self._stream,
                category=CaptureEventCategory.SPILLED_TO_FILE,
                spilled_bytes=self._spilled_bytes,
            )
        )

    def _adopt_spill(self, file: typ.IO[bytes]) -> None:
        """Publish *file*'s handle to both slots teardown reads.

        Called from inside the shielded create, before a cancellation can be
        raised; that is why it exists as one callable rather than two
        assignments at the call site.
        """
        handle = _publish_spill(file)
        self._handle = handle
        self._provisional = handle

    def _promote_provisional_spill(self) -> None:
        """Give the published handle the keep policy the spill has now earned."""
        provisional = self._provisional
        if provisional is not None:
            provisional.keep = self._policy.keep_file
            self._provisional = None

    async def _discard_failed_spill(
        self,
        handle: typ.IO[bytes],
        exc: OSError,
    ) -> None:
        """Remove a spill file that never took the prefix, and stop retrying."""
        # Both calls are housekeeping on the way out of a failed transition,
        # so each tolerates its own failure: the capture has already lost the
        # storage, and a close or unlink that also fails leaves nothing more
        # to lose. They still go through `run`, because closing on the loop is
        # the same blocking call that took the spill off it.
        with contextlib.suppress(OSError):
            await self._io.run(handle.close)
        with contextlib.suppress(OSError):
            await self._io.run(pathlib.Path(handle.name).unlink, missing_ok=True)
        # The file is gone, so the provisional handle published for teardown
        # must not be closed a second time by it. Dropping the reference
        # leaves the sink nothing to clean up for this file; the descriptor
        # was already closed above, so the handle would only duplicate that.
        if self._handle is self._provisional:
            self._handle = None
        self._provisional = None
        self._fail_storage(exc, CaptureEventCategory.STORAGE_WRITE_FAILED)

    def _fail_storage(
        self,
        exc: OSError,
        category: CaptureEventCategory,
    ) -> None:
        """Record a failed storage call and report it against the stream.

        One emitter for both halves of the storage contract — a spill that never
        took the prefix and an append that stopped halfway — because what the
        channel reports is the same either way: which stream, which call
        failed, how much the child had written, and how much had reached the
        file. ``spilled_bytes`` is zero before the transition, so the one body
        states the same fact for both.

        Reporting is also where the failure is absorbed. The caller continues
        down whichever recovery applies; this method never raises, because a
        failure to *retain* output must not become a failure of the command.
        """
        self._failed = type(exc).__name__
        _emit_capture_event(
            CaptureEvent(
                stream=self._stream,
                category=category,
                total_bytes=self._total,
                spilled_bytes=self._spilled_bytes,
                error_type=self._failed,
            )
        )

    async def _append(self, chunk: bytes) -> None:
        """Append one chunk to the spill file, absorbing a write failure.

        The write is all this does; the flush is deferred to
        :meth:`_flush_before_reading`. Nothing reads the file before
        finalization and the handle's own close flushes whatever is left, so a
        flush per chunk would buy nothing and cost a worker round trip per
        read. Running the write off the loop is the whole point of
        :class:`~cuprum._capture_spill_io._SpillIo`: the drain waits for it,
        but the loop stays free to read the child's other stream, honour the
        deadline, and deliver a cancellation.
        """
        if self._failed is not None:
            return
        file = self._file
        if file is None:
            return
        try:
            await self._io.run(file.write, chunk)
        except OSError as exc:
            self._fail_storage(exc, CaptureEventCategory.STORAGE_WRITE_FAILED)
            return
        self._spilled_bytes += len(chunk)

    async def _read_back(self, path: pathlib.Path) -> bytes | None:
        """Return the spill file's bytes, or ``None`` when they cannot be read.

        A byte-exact run asked for the child's own bytes, and a spilled capture
        is the only way to hand back a stream the parent never held whole.
        Reading it does not consume the file, which still outlives the run when
        a policy kept it — and which the caller may, as the documented owner,
        have already read and removed. Because this runs while the result is
        being built, an unreadable file is absorbed here as every other storage
        call is: the record reports ``storage_failed`` and the caller gets the
        path it was pointed at, rather than an exception where a result belongs.

        Returns
        -------
        bytes | None
            The file's contents, or ``None`` when the read failed.
        """
        try:
            return await self._io.run(path.read_bytes)
        except OSError as exc:
            self._fail_storage(exc, CaptureEventCategory.STORAGE_READ_FAILED)
            return None

    def _finalize_buffered(self) -> CaptureFinal:
        """Render a capture that never spilled, as a full capture would."""
        buffered = self._buffer
        retained = b"" if buffered is None else bytes(buffered)
        return CaptureFinal(
            payload=_render(retained, self._settings),
            capture=CapturedStream(
                stream=self._stream,
                policy=policy_kind(self._policy),
                total_bytes=self._total,
                storage_failed=self._failed is not None,
            ),
        )

    async def _flush_before_reading(self) -> None:
        """Flush once, so a read-back sees every byte that was written.

        ``_append`` does not flush per chunk, so this one flush must happen
        even after a failed append: bytes accepted before that failure are
        still buffered, and the byte-mode read-back opens the path separately,
        so skipping it would hide them. A failure is still reported under
        :attr:`~cuprum.capture_events.CaptureEventCategory.STORAGE_FLUSH_FAILED`
        only when no earlier failure explains the capture, and is never raised.
        """
        file = self._file
        if file is None:
            return
        try:
            await self._io.run(file.flush)
        except OSError as exc:
            # The writes since the last flush never reached the file; report
            # the count that did, not every byte handed to ``write``.
            self._spilled_bytes = self._flushed_bytes
            if self._failed is None:
                self._fail_storage(exc, CaptureEventCategory.STORAGE_FLUSH_FAILED)
            return
        self._flushed_bytes = self._spilled_bytes

    async def _finalize_spilled(self, path: pathlib.Path) -> CaptureFinal:
        """Report the spill file in place of text, or its bytes in byte mode."""
        await self._flush_before_reading()
        payload = await self._read_back(path) if self._settings.capture_bytes else None
        handle = self._handle
        return CaptureFinal(
            payload=payload,
            capture=CapturedStream(
                stream=self._stream,
                policy=policy_kind(self._policy),
                total_bytes=self._total,
                storage="file",
                path=path,
                # ``handle is not None`` would be true for every spill, which
                # is not what the field means: a kept file is handed to the
                # caller, so this run is no longer the one that must remove it.
                owned=handle is not None and handle.owned,
                storage_failed=self._failed is not None,
            ),
            handle=handle,
        )
