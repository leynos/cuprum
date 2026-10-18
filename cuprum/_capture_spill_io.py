"""How a file-backed capture makes a blocking call without blocking the run.

A spill is the one capture policy whose retention is a syscall, and the drain
reaches it once per read. Every blocking call therefore runs on a worker thread
rather than on the loop that also drives the child's other stream, its
deadline, and the cancellation that stops it: a write to a full or a slow disk
would otherwise stall all of them for as long as the filesystem took.

The module holds the blocking calls themselves — small, synchronous, and each
raising ``OSError`` rather than deciding anything — and :class:`_SpillIo`, the
ordered seam the accumulator drives them through.

``asyncio.to_thread`` alone is not enough. It starts a *separate* worker for
every call, and the capture's contract is order: the buffered prefix must land
before the chunks that follow it, and the flush must land before the read-back.
``_SpillIo`` serializes one accumulator's calls, so the file's contents are the
order the child wrote them rather than whichever worker the executor scheduled
first.

Two details decide the shape of that seam.

The lock is created inside the first call, not in the constructor, because an
accumulator is built by :class:`~cuprum._stream_config._StreamConfig` outside
any running loop — including a second time, by a pipeline stage reshaped with
:func:`dataclasses.replace` — while :class:`asyncio.Lock` binds to a loop from
its first contention. A lock made eagerly would bind the loop the run never
uses. It is *also* dropped when its loop closes, so an accumulator driven by
successive ``asyncio.run`` calls — as the unit tests drive one — works across
them instead of failing on a lock whose loop is gone.

Each call is shielded. A capture that has handed its bytes to a worker cannot
take them back: ``asyncio.to_thread`` runs the call to completion whatever the
awaiting coroutine does. Raising the cancellation immediately would leave a
worker write in flight while teardown closed and unlinked the file underneath
it. :func:`~cuprum._process_lifecycle._shielded_cleanup` is the tree's existing
answer to that, and reusing it keeps the capture's teardown subject to the same
cancellation contract as every other run path's.
"""

from __future__ import annotations

import asyncio
import tempfile
import typing as typ

from cuprum._process_lifecycle import _shielded_cleanup

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

__all__ = [
    "_SpillIo",
    "_close_spill",
    "_create_spill",
    "_flush_spill",
    "_read_spill",
    "_remove_spill",
    "_write_to_spill",
]


async def _run_off_loop[**P, T](
    call: cabc.Callable[P, T],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Run one blocking *call* on a worker, waiting for it even if cancelled.

    Returns
    -------
    T
        Whatever *call* returned. A failure the call raised propagates
        unchanged, because each caller has its own recovery and they differ.
        A cancellation arriving while the worker runs is re-raised only once
        the call has finished, and any failure alongside it is dropped: the
        capture is being torn down, and a cancellation always wins.
    """
    return await _shielded_cleanup(asyncio.to_thread(call, *args, **kwargs))


class _SpillIo:
    """The ordered, off-loop access one spill file's blocking calls share.

    One instance per file-backed capture, holding the lock that makes its calls
    run one at a time. The caller supplies the call — creating, writing,
    flushing, reading back, closing, unlinking — because which calls a capture
    makes, and what it does when one fails, is the accumulator's policy; this
    object owns only *where* and *in what order* they run.
    """

    __slots__ = ("_lock", "_loop")

    def __init__(self) -> None:
        """Start with no lock; one is made inside the first call."""
        self._lock: asyncio.Lock | None = None
        # The loop `_lock` was made on. Tracked separately from the lock
        # because the run's loop can close between calls, and the lock's own
        # `_loop` is private.
        self._loop: asyncio.AbstractEventLoop | None = None

    def _bound_lock(self) -> asyncio.Lock:
        """Return a lock bound to the running loop, replacing a stale one."""
        loop = asyncio.get_running_loop()
        lock = self._lock
        if lock is None or self._loop is not loop:
            # Made here, inside the loop that will contend it: the accumulator
            # is constructed outside any running loop, and a lock outliving its
            # loop cannot be awaited again. See the module docstring.
            lock = asyncio.Lock()
            self._lock = lock
            self._loop = loop
        return lock

    async def run[**P, T](
        self,
        call: cabc.Callable[P, T],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Run *call* off the loop, after every call queued before it.

        Parameters
        ----------
        call : cabc.Callable[P, T]
            A blocking operation on this capture's file, together with its
            arguments. A filesystem failure must surface as ``OSError``, which
            is what the accumulator absorbs.
        *args : P.args
            The positional arguments *call* takes.
        **kwargs : P.kwargs
            The keyword arguments *call* takes.

        Returns
        -------
        T
            Whatever *call* returned. A failure it raised propagates unchanged,
            after the lock is released; so does a cancellation, once the call
            itself has finished.
        """
        lock = self._bound_lock()
        await lock.acquire()
        try:
            return await _run_off_loop(call, *args, **kwargs)
        finally:
            lock.release()


def _create_spill(directory: pathlib.Path | str | None) -> typ.IO[bytes]:
    """Create the spill file a capture keeps appending to.

    ``delete=False`` is the point, not an oversight: the file must outlive this
    call and every later append, so a ``with`` block would close the descriptor
    the accumulator means to keep writing through. The file is removed by
    :meth:`~cuprum._capture_types.SpillHandle.close` when the stream is neither
    kept nor already unlinked.

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
        The open spill file, positioned at the start. Creating it raises
        whatever the filesystem raises, which the accumulator absorbs.
    """
    return tempfile.NamedTemporaryFile(
        delete=False,
        dir=directory,
    )


def _write_to_spill(file: typ.IO[bytes], data: bytes) -> None:
    """Write *data* to *file*, deliberately without flushing it.

    A failed write raises, as the filesystem raised it; the accumulator turns
    that into a storage-failure record rather than a failed command.
    """
    file.write(data)


def _flush_spill(file: typ.IO[bytes]) -> None:
    """Flush *file*, so its bytes have reached the file itself.

    A failed flush raises, as the filesystem raised it; the accumulator
    reports it under its own event category.
    """
    file.flush()


def _read_spill(path: pathlib.Path) -> bytes:
    """Read *path*.

    Returns
    -------
    bytes
        The file's whole contents. A file that cannot be read raises, which the
        accumulator absorbs.
    """
    return path.read_bytes()


def _close_spill(file: typ.IO[bytes]) -> None:
    """Close *file*, flushing whatever its buffer still holds.

    The close is the last chance for a buffered write to reach the file, which
    is why it has to be ordered after every write and off the loop with them.
    A failed close raises, and every caller treats it as housekeeping.
    """
    file.close()


def _remove_spill(path: pathlib.Path) -> None:
    """Unlink *path*, tolerating a file that is already gone.

    ``missing_ok`` is what makes a teardown that races the normal path's own
    removal harmless. Every other failure raises, and every caller treats it as
    housekeeping.
    """
    path.unlink(missing_ok=True)
