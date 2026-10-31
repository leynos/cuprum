"""Tearing down the capture accumulators a finished run still holds.

A spill file has two removal paths, and this module is the second of them. The
first is the run's own result-construction site, which closes the handle every
drain result carried; that path covers every drain that settled, because a
settled drain always reports what it retained.

The path here covers the drains that did **not** settle. A drain cancelled
while awaiting its line sink re-raises without finalizing, so no result is
built and the handle it would have carried never reaches a caller; the
accumulator the drain was reading into is then the last reference to the file.
Both paths call :meth:`cuprum._capture_types.SpillHandle.close`, which is
idempotent, so a run whose file was already removed by the first path is
cleaned up again here without a race.

Keeping this out of the single-command and pipeline teardown modules is what
lets both use it: the registry a pipeline fills and the fixed pair a command
builds are the same obligation, and a second implementation of "close every
sink and remove every owned file" is exactly the kind of duplication that lets
one path keep a temporary directory clean while the other leaks into it.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._stream_drain_finish import _finalize_drain_handles

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._capture_types import CaptureSink
    from cuprum._stream_drain_finish import _CapturedDrain

__all__ = [
    "_cleanup_capture_sinks",
    "_finalize_capture_handles",
]


async def _cleanup_capture_sinks(
    sinks: cabc.Iterable[CaptureSink | None],
) -> None:
    """Release every capture accumulator a finished run still holds.

    Every sink is given its turn even when an earlier one fails: a sink that
    cannot remove its own file must not leave another stream's file behind.
    The failure is collected rather than raised in place, and the first one is
    re-raised once the loop has finished, so the caller still learns that a
    file may remain.

    Parameters
    ----------
    sinks : cabc.Iterable[CaptureSink | None]
        The accumulators the run created, in any order. ``None`` entries are
        skipped, so a caller can pass a fixed per-stream pair whose streams
        were not captured.

    Raises
    ------
    BaseException
        The first failure any sink reported, once every sink has been asked.
    """  # ruff: ignore[docstring-extraneous-exception] - a sink's own failure is re-raised after the loop
    results = await asyncio.gather(
        *(sink.cleanup() for sink in sinks if sink is not None),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result


async def _finalize_capture_handles(
    drains: cabc.Iterable[_CapturedDrain | None],
    sinks: cabc.Iterable[CaptureSink | None],
) -> None:
    """Close a run's spill files, by result first and by accumulator second.

    The two steps answer different questions and both are needed. The drain
    results are authoritative for what the run captured, so they are closed
    first, while the accumulators catch whatever those results do not mention —
    a drain cancelled mid-await, whose file only the sink still knows about.

    Parameters
    ----------
    drains : cabc.Iterable[_CapturedDrain | None]
        Every drain result the run produced, settled or partial. ``None``
        entries are skipped, which is what a pipeline stage with no capture
        contributes: its per-stage tuple is ``None`` for a stream it never
        drained, and the caller concatenates those tuples without filtering so
        the passes stay in stage order.
    sinks : cabc.Iterable[CaptureSink | None]
        The accumulators those drains read into.
    """
    await _finalize_drain_handles(*drains)
    await _cleanup_capture_sinks(sinks)
