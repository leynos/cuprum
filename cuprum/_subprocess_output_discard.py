"""Discard a stopped child's output within the capture drain's EOF grace.

Split from ``cuprum._subprocess_startup_abandon``, which owns the reclaim
sequence but would otherwise exceed the repository's module-length ceiling.
The seam is the one the two responsibilities already drew: this module owns
*how long* reading a stopped child's pipes may take and how that wait is
bounded, while the reclaim owns *when* the discard runs and what follows it.

One window covers both readers, as in the drain itself
(:func:`cuprum._stream_drain._await_eof_grace`): two pipes kept open by a
descendant cost one grace, not one each — the documented bound. A read that is
still pending when the window closes is cancelled and gathered, and the discard
reports either the first ordinary read failure or the expiry, preferring the
failure so an operator still sees why the discard ended.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._stream_drain import _CAPTURE_EOF_GRACE_S

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# Chunk size for the discard read. Reading to EOF in bounded pieces rather than
# with a bare ``read()`` keeps a child that wrote a large backlog before it was
# stopped from being copied into memory all at once; the bytes are discarded.
_DISCARD_CHUNK_BYTES = 64 * 1024

__all__ = ["_discard_outputs"]


async def _discard_outputs(process: asyncio.subprocess.Process) -> None:
    """Discard both output streams within the capture drain's single grace."""
    streams = tuple(
        stream for stream in (process.stdout, process.stderr) if stream is not None
    )
    if not streams:
        return
    reads = tuple(asyncio.ensure_future(_discard_to_eof(stream)) for stream in streams)
    await _discard_within_grace(reads)


async def _discard_within_grace(reads: cabc.Sequence[asyncio.Task[None]]) -> None:
    """Wait within the capture drain's grace, settle the reads, then report.

    Raises
    ------
    TimeoutError
        When the grace expired with a read still pending and none failed.
    """
    try:
        _, pending = await asyncio.wait(reads, timeout=_CAPTURE_EOF_GRACE_S)
    finally:
        await _settle_reads(reads)
    failure = _first_read_failure(reads)
    if failure is not None:
        raise failure
    if pending:
        raise TimeoutError


async def _settle_reads(reads: cabc.Sequence[asyncio.Task[None]]) -> None:
    """Cancel and gather every read, so none outlives the discard."""
    for task in reads:
        task.cancel()
    await asyncio.gather(*reads, return_exceptions=True)


def _first_read_failure(reads: cabc.Sequence[asyncio.Task[None]]) -> Exception | None:
    """Return the first ordinary read failure, in stream order.

    Only an ordinary ``Exception`` counts, and the failure is preferred to the
    grace's expiry so the operator still sees why the discard ended.

    Returns
    -------
    Exception | None
        The first ordinary failure in stream order, or ``None`` if none.
    """
    for task in reads:
        if task.cancelled():
            continue
        error = task.exception()
        if isinstance(error, Exception):
            return error
    return None


async def _discard_to_eof(stream: asyncio.StreamReader) -> None:
    """Read a stream to end-of-file, discarding it, in bounded pieces."""
    chunk = await stream.read(_DISCARD_CHUNK_BYTES)
    while chunk:
        chunk = await stream.read(_DISCARD_CHUNK_BYTES)
