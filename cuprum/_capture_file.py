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
- A failure to *append* after the spill stops writing and marks the capture
  failed. The file still holds whatever reached it before the failure and is
  still removed by cleanup, so even a failed run leaves nothing in the
  temporary directory; ``storage_failed`` on the record is what tells the
  caller the retained capture is incomplete rather than merely short.
"""

from __future__ import annotations

import contextlib
import pathlib
import tempfile
import typing as typ

from cuprum._capture_decode import _DecodeSettings, _render
from cuprum._capture_types import CaptureFinal, SpillHandle
from cuprum.capture_events import CaptureEvent, CaptureEventCategory
from cuprum.capture_observation import _emit_capture_event
from cuprum.capture_policy import CapturedStream, FileBackedCapture, policy_kind

if typ.TYPE_CHECKING:
    from cuprum.capture_policy import CaptureStreamName

__all__ = [
    "_FileAccumulator",
]


def _open_spill_file(directory: pathlib.Path | str | None) -> typ.IO[bytes]:
    """Create the spill file a capture keeps appending to.

    ``delete=False`` is the point, not an oversight: the file must outlive this
    call and every later append, so a ``with`` block would close the descriptor
    the accumulator means to keep writing through. The file is removed by
    :meth:`SpillHandle.close` when the stream is neither kept nor already
    unlinked.

    Returning the handle rather than assigning it at the call site is what
    makes that intent legible to a reader and to the linter: the resource is
    this function's *result*, not a temporary bound to a name in the middle of
    a longer method, and the obligation to release it passed to the caller
    along with it.

    Parameters
    ----------
    directory : pathlib.Path | str | None
        Directory to create the file in, or ``None`` for the process's
        temporary directory. The ``str`` arm is admitted to match the declared
        type of :attr:`~cuprum.capture_policy.FileBackedCapture.directory`,
        which is ``Path | str | None`` so a caller may pass either. Its
        ``__post_init__`` normalizes to :class:`~pathlib.Path` before this is
        ever reached, but that normalization is invisible in the field's
        annotation, so narrowing here would only force a cast at the call site
        that asserted the very thing the annotation cannot promise.

    Returns
    -------
    typ.IO[bytes]
        The open spill file, positioned at the start.
    """
    return tempfile.NamedTemporaryFile(
        delete=False,
        dir=directory,
    )


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
        "_handle",
        "_path",
        "_policy",
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
        self._buffer: bytearray | None = bytearray()
        # Whichever type :func:`_open_spill_file` hands back, which is the
        # temporary-file wrapper rather than the buffered file it forwards to;
        # the two annotations must agree or every assignment between them
        # fails the typechecker.
        self._file: typ.IO[bytes] | None = None
        # One handle for the life of the capture, not one per query: the
        # normal path closes it from the finalized result while teardown may
        # also close it from the sink, and a fresh object each time would give
        # those two paths independent ``closed`` flags.
        self._handle: SpillHandle | None = None
        self._path: pathlib.Path | None = None
        self._total = 0
        self._spilled_bytes = 0
        # The *name* of the class behind a storage failure, never the
        # exception: it is reported through an event channel whose consumers
        # treat fields as low-cardinality labels.
        self._failed: str | None = None

    def absorb(self, chunk: bytes) -> None:
        """Buffer the chunk, spill it, or append it, by where the capture is."""
        self._total += len(chunk)
        if self._path is not None:
            self._append(chunk)
            return
        buffered = self._buffer
        if buffered is None:
            return
        buffered.extend(chunk)
        if self._should_spill(len(buffered)):
            self._spill()

    def finalize(self) -> CaptureFinal:
        """Render spilled or in-memory bytes, with the record describing them."""
        path = self._path
        if path is not None:
            return self._finalize_spilled(path)
        return self._finalize_buffered()

    async def cleanup(self) -> None:
        """Close this run's spill file, removing it unless the caller kept it."""
        handle = self._handle
        if handle is not None:
            await handle.close(remove=not self._policy.keep_file)

    def _should_spill(self, buffered: int) -> bool:
        """Whether *buffered* bytes have outgrown the configured threshold."""
        threshold = self._policy.spill_threshold_bytes
        return threshold is None or buffered > threshold

    def _spill(self) -> None:
        """Move the buffered prefix into a new file and append there after.

        A failure here leaves the stream on the heap: nothing was written
        anywhere else, so buffering continues and the caller still reads
        complete text. The half-made file is removed and the sink stops
        retrying, so a full or read-only directory costs one syscall rather
        than one per chunk.
        """
        buffered = self._buffer
        if buffered is None:
            return
        try:
            handle = _open_spill_file(self._policy.directory)
        except OSError as exc:
            self._abandon_spill(exc, CaptureEventCategory.STORAGE_WRITE_FAILED)
            return
        try:
            handle.write(buffered)
            handle.flush()
        except OSError as exc:
            self._discard_failed_spill(handle, exc)
            return
        self._spilled_bytes = len(buffered)
        self._file = handle
        path = pathlib.Path(handle.name)
        self._path = path
        self._handle = SpillHandle(path=path, file=handle, keep=self._policy.keep_file)
        self._buffer = None
        _emit_capture_event(
            CaptureEvent(
                stream=self._stream,
                category=CaptureEventCategory.SPILLED_TO_FILE,
                spilled_bytes=self._spilled_bytes,
            )
        )

    def _discard_failed_spill(
        self,
        handle: typ.IO[bytes],
        exc: OSError,
    ) -> None:
        """Remove a spill file that never took the prefix, and stop retrying."""
        with contextlib.suppress(OSError):
            handle.close()
        with contextlib.suppress(OSError):
            pathlib.Path(handle.name).unlink(missing_ok=True)
        self._abandon_spill(exc, CaptureEventCategory.STORAGE_WRITE_FAILED)

    def _abandon_spill(self, exc: OSError, category: CaptureEventCategory) -> None:
        """Record a failed spill transition and keep buffering in memory."""
        self._failed = type(exc).__name__
        _emit_capture_event(
            CaptureEvent(
                stream=self._stream,
                category=category,
                total_bytes=self._total,
                error_type=self._failed,
            )
        )

    def _append(self, chunk: bytes) -> None:
        """Append one chunk to the spill file, absorbing any failure."""
        if self._failed is not None:
            return
        file = self._file
        if file is None:
            return
        try:
            file.write(chunk)
        except OSError as exc:
            self._fail_append(exc, CaptureEventCategory.STORAGE_WRITE_FAILED)
            return
        try:
            file.flush()
        except OSError as exc:
            self._fail_append(exc, CaptureEventCategory.STORAGE_FLUSH_FAILED)
            return
        self._spilled_bytes += len(chunk)

    def _fail_append(self, exc: OSError, category: CaptureEventCategory) -> None:
        """Stop writing after a failed append, keeping the spilled prefix.

        Writing stops rather than being retried per chunk: a directory that
        has become full or read-only does not recover mid-run, and one failed
        syscall per chunk would turn a bounded capture into an unbounded one.
        The file is left for cleanup and the record reports ``storage_failed``,
        so the caller can tell the capture is incomplete.
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

    def _finalize_spilled(self, path: pathlib.Path) -> CaptureFinal:
        """Report the spill file in place of text, or its bytes in byte mode."""
        # A byte-exact run asked for the child's own bytes, and a spilled
        # capture is the only way to hand back a stream the parent never held
        # whole. Reading it does not consume the file, which still outlives
        # the run when a policy kept it.
        payload = path.read_bytes() if self._settings.capture_bytes else None
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
