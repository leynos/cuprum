"""Regression coverage for resilient subprocess exit waiting."""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._process_exit import (
    _PROCESS_EXIT_INITIAL_POLL_INTERVAL,
    _PROCESS_EXIT_MAX_POLL_INTERVAL,
    _await_process_exit,
)

if typ.TYPE_CHECKING:
    import pytest


class _PublishedExitProcess:
    """Process double whose waiter strands after publishing an exit code."""

    def __init__(self) -> None:
        """Start without a published return code."""
        self.returncode: int | None = None

    async def wait(self) -> int:
        """Publish success, then model asyncio's orphan pending wait future."""
        for _ in range(3):
            await asyncio.sleep(0)
        self.returncode = 0
        await asyncio.Event().wait()
        return 0


class _StrandedWaitProcess:
    """Process double whose wait task only ends through cancellation."""

    def __init__(self) -> None:
        """Start without an exit code or completed wait task."""
        self.returncode: int | None = None
        self.wait_cancelled = False
        self._waiter = asyncio.Event()

    async def wait(self) -> int:
        """Wait forever unless cleanup cancels the stranded task."""
        try:
            await self._waiter.wait()
        except asyncio.CancelledError:
            self.wait_cancelled = True
            raise
        return 0


def test_await_process_exit_accepts_published_returncode() -> None:
    """A published return code wins over an orphan pending waiter."""

    async def run_case() -> int:
        """Wait for the stranded process double without hanging."""
        process = typ.cast("asyncio.subprocess.Process", _PublishedExitProcess())
        return await asyncio.wait_for(_await_process_exit(process), timeout=0.5)

    assert asyncio.run(run_case()) == 0, (
        "a published exit code must complete a stranded process waiter"
    )


def test_await_process_exit_backs_off_while_waiter_is_stranded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lost-wakeup fallback backs off and cancels its losing waiter."""

    async def run_case() -> tuple[int, list[float], bool]:
        """Publish an exit code after enough polls to reach the backoff cap."""
        process = _StrandedWaitProcess()
        intervals: list[float] = []
        original_sleep = asyncio.sleep

        async def record_sleep(interval: float) -> None:
            """Record each delay before eventually publishing the exit code."""
            intervals.append(interval)
            if len(intervals) == 10:
                process.returncode = 0
            await original_sleep(0)

        monkeypatch.setattr("cuprum._process_exit.sleep", record_sleep)
        exit_code = await _await_process_exit(
            typ.cast("asyncio.subprocess.Process", process)
        )
        return exit_code, intervals, process.wait_cancelled

    exit_code, intervals, wait_cancelled = asyncio.run(run_case())

    assert exit_code == 0, "the published exit code must complete the fallback"
    assert intervals == [
        min(
            _PROCESS_EXIT_INITIAL_POLL_INTERVAL * 2**index,
            _PROCESS_EXIT_MAX_POLL_INTERVAL,
        )
        for index in range(10)
    ], "long-lived waiters must use capped exponential backoff"
    assert wait_cancelled, "completion must cancel the stranded process waiter"
