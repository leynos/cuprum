"""Closing the capture handles a completed ``lines()`` run still owns.

Part of the ``cuprum._line_stream`` package. The coordinator assembles a
completed run's result; this module owns the one cleanup that result owes,
split out so the coordinator stays about coordination rather than about spill
files.
"""

from __future__ import annotations

import typing as typ

from cuprum._capture_cleanup import _finalize_capture_handles
from cuprum._process_lifecycle import _shielded_cleanup

if typ.TYPE_CHECKING:
    from cuprum._line_stream.line_queue import _LineStreamRun
    from cuprum._stream_drain_finish import _CapturedDrain

__all__ = ["_finalize_line_stream_captures"]


async def _finalize_line_stream_captures(
    run: _LineStreamRun,
    stdout_drain: _CapturedDrain,
    stderr_drain: _CapturedDrain,
) -> None:
    """Close the spill files a completed ``lines()`` run still owns.

    ``run()`` closes its handles right after collecting the streams, and the
    ``lines()`` success path reaches the same place — every consumer has
    settled and the drain results are in hand — so it owes the same removal.
    Without this, a ``FileBackedCapture`` would return a record reporting
    ``owned=True`` for a file that outlives the run, with nothing left to
    remove it.

    Shielded because the drains are already settled: a cancellation landing
    between the collect and the close would leave the file on disk while the
    caller was still about to be handed the result.
    """
    await _shielded_cleanup(
        _finalize_capture_handles(
            (stdout_drain, stderr_drain),
            run.tasks.captures,
        )
    )
