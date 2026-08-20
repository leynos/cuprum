"""Regression coverage for the pipeline wait path's lost-wakeup fallback.

The per-stage wait tasks built by ``_PipelineWaitState.from_processes``
inherit the bounded polling fallback from ``_await_process_exit``. When
asyncio's ``process.wait()`` is stranded but the transport has still
published a return code, the affected wait task completes from that published
code. This module covers the multi-stage ownership path that the
helper-level ``test_process_exit`` suite deliberately leaves to pipeline
bookkeeping.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._pipeline_wait import _PipelineWaitState
from cuprum._process_exit import (
    _PROCESS_EXIT_INITIAL_POLL_INTERVAL,
    _PROCESS_EXIT_MAX_POLL_INTERVAL,
)

if typ.TYPE_CHECKING:
    import pytest


class _StrandedWaitPipelineProcess:
    """Pipeline process double whose ``wait()`` only ends via cancellation.

    This mirrors the lost-wakeup asyncio subprocess condition: the transport
    has not published a return code, and the asyncio waiter stays pending
    until the owning wait task is cancelled.
    """

    def __init__(self, *, pid: int) -> None:
        """Start without a published exit code or a completed waiter."""
        self.pid = pid
        self.returncode: int | None = None
        self.wait_cancelled = False
        self._waiter = asyncio.Event()

    async def wait(self) -> int:
        """Wait forever unless the owning wait task is cancelled."""
        try:
            await self._waiter.wait()
        except asyncio.CancelledError:
            self.wait_cancelled = True
            raise
        return self.returncode if self.returncode is not None else 0


def _is_capped_backoff_interval(interval: float) -> bool:
    """Return whether *interval* is a valid capped exponential-backoff value."""
    value = _PROCESS_EXIT_INITIAL_POLL_INTERVAL
    while value < _PROCESS_EXIT_MAX_POLL_INTERVAL:
        if interval == value:
            return True
        value *= 2
    return interval == _PROCESS_EXIT_MAX_POLL_INTERVAL


def test_pipeline_wait_recovers_lost_wakeup_from_published_returncode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-stage wait tasks recover stranded waiters via capped backoff.

    Three stages start with no published exit code and a stranded ``wait()``.
    One stage publishes a return code after a controlled number of patched
    ``asyncio.sleep`` calls, so its pipeline wait task completes from the
    published code. The two remaining pending wait tasks are cancelled and
    drained during the pipeline cleanup that follows.
    """

    async def run_case() -> tuple[int, list[float], bool, bool, bool]:
        """Recover one stranded stage, then drain the other two in cleanup."""
        processes = [_StrandedWaitPipelineProcess(pid=pid) for pid in (1, 2, 3)]
        intervals: list[float] = []
        original_sleep = asyncio.sleep

        async def record_sleep(interval: float) -> None:
            """Record each poll interval and publish stage two after 15 polls."""
            intervals.append(interval)
            if len(intervals) == 15:
                processes[1].returncode = 0
            await original_sleep(0)

        monkeypatch.setattr("cuprum._process_exit.asyncio.sleep", record_sleep)
        state = _PipelineWaitState.from_processes(
            typ.cast("list[asyncio.subprocess.Process]", processes),
            started_at=[0.0, 0.0, 0.0],
        )
        affected_result = await asyncio.wait_for(state.wait_tasks[1], timeout=10)
        for task in (state.wait_tasks[0], state.wait_tasks[2]):
            task.cancel()
        await asyncio.gather(*state.wait_tasks, return_exceptions=True)
        return (
            affected_result,
            intervals,
            processes[0].wait_cancelled,
            processes[1].wait_cancelled,
            processes[2].wait_cancelled,
        )

    result, intervals, first_cancelled, affected_cancelled, last_cancelled = (
        asyncio.run(run_case())
    )

    assert len(intervals) >= 15, "the fallback must poll before publishing"
    assert intervals[0] == _PROCESS_EXIT_INITIAL_POLL_INTERVAL, (
        "polling must start at the initial backoff interval"
    )
    assert intervals[-1] > _PROCESS_EXIT_INITIAL_POLL_INTERVAL, (
        "long-lived waiters must back off beyond the initial interval"
    )
    assert all(_is_capped_backoff_interval(interval) for interval in intervals), (
        "every recorded interval must be a capped exponential-backoff value"
    )
    assert all(interval <= _PROCESS_EXIT_MAX_POLL_INTERVAL for interval in intervals), (
        "polling must never exceed the maximum backoff interval"
    )

    assert result == 0, "the affected wait task must complete from its published code"
    assert affected_cancelled, (
        "the affected task must cancel its stranded asyncio waiter"
    )
    assert first_cancelled, (
        "pipeline cleanup must cancel the first remaining stranded wait task"
    )
    assert last_cancelled, (
        "pipeline cleanup must cancel the last remaining stranded wait task"
    )
