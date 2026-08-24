"""Regression coverage for the pipeline wait path's lost-wakeup fallback.

The per-stage wait tasks built by ``_PipelineWaitState.from_processes``
inherit the bounded polling fallback from ``_await_process_exit``. When
asyncio's ``process.wait()`` is stranded but the transport has still
published a return code, the affected wait task completes from that published
code. Driving ``_wait_for_pipeline`` exercises the real multi-stage ownership
path: the fail-fast teardown terminates the remaining stages, whose stranded
asyncio waiters are cancelled and drained while the polled return codes are
recorded with capped exponential backoff.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._pipeline_types import _StageWaitContext
from cuprum._pipeline_wait import _PipelineWaitResult, _wait_for_pipeline
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
    until the owning wait task is cancelled. Terminating or killing the
    process publishes a return code, exactly as the real transport does after
    a signal, without ever completing the stranded asyncio waiter.
    """

    def __init__(self, *, pid: int) -> None:
        """Start without a published exit code or a completed waiter."""
        self.pid = pid
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_cancelled = False
        self._waiter = asyncio.Event()

    def terminate(self) -> None:
        """Record termination and publish the post-signal exit code."""
        self.terminate_calls += 1
        if self.returncode is None:
            self.returncode = -15

    def kill(self) -> None:
        """Record the kill and publish the post-SIGKILL exit code."""
        self.kill_calls += 1
        if self.returncode is None:
            self.returncode = -9

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


def test_wait_for_pipeline_recovers_lost_wakeup_before_fail_fast_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-fast cleanup drains stranded waiters after a polled exit code.

    Three stages start with no published exit code and a stranded ``wait()``.
    One stage publishes a non-zero return code after a controlled number of
    patched ``asyncio.sleep`` calls, so its pipeline wait task completes from
    the published code and triggers fail-fast teardown. The remaining stages
    are terminated, their stranded asyncio waiters cancelled and drained, and
    the recorded poll intervals verified as capped exponential backoff.
    """

    async def run_case() -> tuple[
        _PipelineWaitResult,
        list[float],
        list[bool],
        list[int],
    ]:
        """Run the pipeline wait to completion with the patched poll loop."""
        processes = [_StrandedWaitPipelineProcess(pid=pid) for pid in (1, 2, 3)]
        intervals: list[float] = []
        original_sleep = asyncio.sleep

        async def record_sleep(interval: float) -> None:
            """Record each poll interval and fail stage two after ten polls."""
            intervals.append(interval)
            if len(intervals) == 10:
                processes[1].returncode = 7
            await original_sleep(0)

        monkeypatch.setattr("cuprum._process_exit.asyncio.sleep", record_sleep)
        result = await asyncio.wait_for(
            _wait_for_pipeline(
                typ.cast("list[asyncio.subprocess.Process]", processes),
                pipe_tasks=[],
                cancel_grace=1.0,
                stages=_StageWaitContext(started_at=(0.0, 0.0, 0.0)),
            ),
            timeout=10,
        )
        return (
            result,
            intervals,
            [process.wait_cancelled for process in processes],
            [process.terminate_calls for process in processes],
        )

    result, intervals, cancelled, terminate_calls = asyncio.run(run_case())

    assert result.exit_codes == (-15, 7, -15), (
        "the affected stage must complete from its published code and the "
        "remaining stages from the post-termination codes"
    )
    assert result.failure_index == 1, (
        "the fail-fast teardown must record the published failing stage"
    )
    assert terminate_calls == [1, 0, 1], (
        "fail-fast cleanup must terminate both remaining stages exactly once"
    )
    assert all(cancelled), "cleanup must cancel and drain every stranded asyncio waiter"

    assert len(intervals) >= 10, "the fallback must poll before publishing"
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
