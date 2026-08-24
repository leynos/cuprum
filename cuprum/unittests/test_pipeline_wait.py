"""Unit tests for the internal ``_wait_for_pipeline`` fail-fast behaviour."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import pytest

from cuprum._process_exit import (
    _PROCESS_EXIT_INITIAL_POLL_INTERVAL,
    _PROCESS_EXIT_MAX_POLL_INTERVAL,
)
from cuprum._testing import (
    _PipelineWaitResult,
    _StageWaitContext,
    _wait_for_pipeline,
)

if typ.TYPE_CHECKING:
    from collections import abc as cabc


class _StubPipelineWaitProcess:
    """Stub process modelling exit codes and readiness for wait tests."""

    def __init__(
        self,
        *,
        pid: int,
        exit_code: int,
        ready: asyncio.Event | None = None,
    ) -> None:
        """Initialize the stub with its PID, exit code, and ready event."""
        self.pid = pid
        self.returncode: int | None = None
        self.stdout = None
        self.stderr = None
        self.stdin = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self._exit_code = exit_code
        self._ready = ready

    def terminate(self) -> None:
        """Record termination and unblock any pending wait."""
        self.terminate_calls += 1
        self._exit_code = -15
        if self._ready is not None:
            self._ready.set()

    def kill(self) -> None:
        """Record the kill and unblock any pending wait."""
        self.kill_calls += 1
        self._exit_code = -9
        if self._ready is not None:
            self._ready.set()

    async def wait(self) -> int:
        """Await readiness if set, then return the recorded exit code."""
        if self._ready is not None:
            await self._ready.wait()
        await asyncio.sleep(0)
        if self.returncode is None:
            self.returncode = self._exit_code
        return self.returncode


class _PublishedExitPipelineProcess(_StubPipelineWaitProcess):
    """Stub that publishes an exit code while leaving ``wait`` pending."""

    async def wait(self) -> int:
        """Publish the exit code, then model a stranded asyncio wait future."""
        for _ in range(3):
            await asyncio.sleep(0)
        self.returncode = self._exit_code
        await asyncio.Event().wait()
        return self.returncode


class _StrandedPipelineWaitProcess:
    """Process double whose waiter remains pending until cancellation."""

    def __init__(self, *, pid: int) -> None:
        """Start with no published exit code or completed waiter."""
        self.pid = pid
        self.returncode: int | None = None
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_cancelled = False
        self._waiter = asyncio.Event()

    def terminate(self) -> None:
        """Record an unexpected pipeline termination request."""
        self.terminate_calls += 1

    def kill(self) -> None:
        """Record an unexpected pipeline kill request."""
        self.kill_calls += 1

    async def wait(self) -> int:
        """Remain pending until the losing waiter is cancelled."""
        try:
            await self._waiter.wait()
        except asyncio.CancelledError:
            self.wait_cancelled = True
            raise
        return 0


@dc.dataclass(frozen=True, slots=True)
class _StrandedPipelineWaitCase:
    """Capture one completed stranded pipeline-wait scenario."""

    result: _PipelineWaitResult
    processes: list[_StrandedPipelineWaitProcess]
    polling_intervals: dict[asyncio.Task[object], list[float]]


@dc.dataclass(slots=True)
class _PollingRecorder:
    """Record task-local polls and publish controlled pipeline completion."""

    processes: list[_StrandedPipelineWaitProcess]
    original_sleep: cabc.Callable[[float], cabc.Awaitable[None]]
    required_poll_rounds: int = 3
    polling_intervals: dict[asyncio.Task[object], list[float]] = dc.field(
        default_factory=dict
    )

    async def __call__(self, interval: float) -> None:
        """Record one poll, then yield without waiting for its interval."""
        polling_task = asyncio.current_task()
        assert polling_task is not None, "each poll must run in an asyncio task"
        intervals = self.polling_intervals.setdefault(polling_task, [])
        intervals.append(interval)
        if len(self.polling_intervals) == len(self.processes) and all(
            len(recorded) >= self.required_poll_rounds
            for recorded in self.polling_intervals.values()
        ):
            for process in self.processes:
                process.returncode = 0
        await self.original_sleep(0)


async def _exercise_wait_for_pipeline(
    exit_codes: tuple[int, int, int],
    ready_stages: frozenset[int],
) -> tuple[
    _StubPipelineWaitProcess,
    _StubPipelineWaitProcess,
    _StubPipelineWaitProcess,
    _PipelineWaitResult,
]:
    """Execute _wait_for_pipeline with stub processes and custom exit scenarios."""
    events = [asyncio.Event() for _ in range(3)]
    for idx in ready_stages:
        events[idx].set()

    processes = [
        _StubPipelineWaitProcess(pid=i + 1, exit_code=exit_codes[i], ready=events[i])
        for i in range(3)
    ]

    result = await _wait_for_pipeline(
        typ.cast("list[asyncio.subprocess.Process]", processes),
        pipe_tasks=[],
        cancel_grace=0.01,
        stages=_StageWaitContext(started_at=(0.0, 0.0, 0.0)),
    )

    return processes[0], processes[1], processes[2], result


def test_wait_for_pipeline_accepts_published_returncode() -> None:
    """Complete when a process publishes its code but strands ``wait``."""
    process = _PublishedExitPipelineProcess(pid=1, exit_code=0)

    result = asyncio.run(
        asyncio.wait_for(
            _wait_for_pipeline(
                typ.cast("list[asyncio.subprocess.Process]", [process]),
                pipe_tasks=[],
                cancel_grace=0.01,
                started_at=[0.0],
            ),
            timeout=0.5,
        ),
    )

    assert result.exit_codes == (0,), "pipeline should observe the published exit code"


async def _run_stranded_pipeline_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> _StrandedPipelineWaitCase:
    """Run a three-stage pipeline whose process waiters remain stranded."""
    processes = [_StrandedPipelineWaitProcess(pid=index) for index in range(3)]
    recorder = _PollingRecorder(processes, asyncio.sleep)
    monkeypatch.setattr("cuprum._process_exit.sleep", recorder)
    result = await _wait_for_pipeline(
        typ.cast("list[asyncio.subprocess.Process]", processes),
        pipe_tasks=[],
        cancel_grace=0.01,
        stages=_StageWaitContext(started_at=(0.0,) * len(processes)),
    )
    return _StrandedPipelineWaitCase(
        result=result,
        processes=processes,
        polling_intervals=recorder.polling_intervals,
    )


def _assert_stranded_pipeline_wait(case: _StrandedPipelineWaitCase) -> None:
    """Assert successful recovery from every stranded process waiter."""
    assert case.result.exit_codes == (0, 0, 0), "all published exits must succeed"
    assert case.result.failure_index is None, "successful stages must not fail fast"
    assert all(process.wait_cancelled for process in case.processes), (
        "each stranded process waiter must be cancelled after publication"
    )
    assert all(process.terminate_calls == 0 for process in case.processes), (
        "successful stages must not receive terminate requests"
    )
    assert all(process.kill_calls == 0 for process in case.processes), (
        "successful stages must not receive kill requests"
    )
    assert len(case.polling_intervals) == len(case.processes), (
        "each pipeline stage must own a distinct polling task"
    )
    for intervals in case.polling_intervals.values():
        assert intervals[0] == _PROCESS_EXIT_INITIAL_POLL_INTERVAL, (
            "each polling task must begin with the initial interval"
        )
        assert intervals[1:] == [
            min(
                _PROCESS_EXIT_INITIAL_POLL_INTERVAL * 2**index,
                _PROCESS_EXIT_MAX_POLL_INTERVAL,
            )
            for index in range(1, len(intervals))
        ], "later polling intervals must use capped exponential backoff"
        assert all(
            interval <= _PROCESS_EXIT_MAX_POLL_INTERVAL for interval in intervals
        ), "no polling interval may exceed the configured cap"


def _assert_stage_terminated(
    process: _StubPipelineWaitProcess,
    *,
    should_terminate: bool,
) -> None:
    """Assert whether a process was terminated during fail-fast."""
    if should_terminate:
        assert process.terminate_calls == 1, (
            f"Process {process.pid} should be terminated"
        )
    else:
        assert process.terminate_calls == 0, (
            f"Process {process.pid} should not be terminated"
        )


def _assert_pipeline_failure(
    result: _PipelineWaitResult,
    *,
    failure_index: int | None,
    exit_codes: tuple[int, ...],
) -> None:
    """Assert pipeline wait result failure metadata."""
    assert result.failure_index == failure_index, (
        "the failing stage index must match the scenario"
    )
    assert result.exit_codes == exit_codes, (
        "recorded exit codes must match the scenario"
    )


@dc.dataclass(frozen=True, slots=True)
class _FailFastScenario:
    """Configuration for a fail-fast pipeline test scenario."""

    exit_codes: tuple[int, int, int]
    ready_stages: frozenset[int]
    expected_failure_index: int
    expected_exit_codes: tuple[int, int, int]
    terminated_stages: frozenset[int]


@pytest.mark.parametrize(
    "scenario",
    [
        pytest.param(
            _FailFastScenario(
                exit_codes=(7, 0, 0),
                ready_stages=frozenset([0]),
                expected_failure_index=0,
                expected_exit_codes=(7, -15, -15),
                terminated_stages=frozenset([1, 2]),
            ),
            id="early-stage-failure-terminates-downstream",
        ),
        pytest.param(
            _FailFastScenario(
                exit_codes=(0, 3, 0),
                ready_stages=frozenset([1]),
                expected_failure_index=1,
                expected_exit_codes=(-15, 3, -15),
                terminated_stages=frozenset([0, 2]),
            ),
            id="middle-stage-failure-terminates-all-other-stages",
        ),
        pytest.param(
            _FailFastScenario(
                exit_codes=(0, 0, 5),
                ready_stages=frozenset([2]),
                expected_failure_index=2,
                expected_exit_codes=(-15, -15, 5),
                terminated_stages=frozenset([0, 1]),
            ),
            id="last-stage-failure-terminates-upstream",
        ),
    ],
)
def test_wait_for_pipeline_fail_fast_scenarios(
    scenario: _FailFastScenario,
) -> None:
    """Validate fail-fast termination behaviour across different failure scenarios.

    Tests that:
    - Any failed stage terminates every other still-running stage
    """
    p0, p1, p2, result = asyncio.run(
        _exercise_wait_for_pipeline(
            exit_codes=scenario.exit_codes,
            ready_stages=scenario.ready_stages,
        ),
    )

    _assert_pipeline_failure(
        result,
        failure_index=scenario.expected_failure_index,
        exit_codes=scenario.expected_exit_codes,
    )

    for idx, process in enumerate([p0, p1, p2]):
        _assert_stage_terminated(
            process,
            should_terminate=(idx in scenario.terminated_stages),
        )


def test_wait_for_pipeline_polls_stranded_waiters_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Published exit codes complete each pipeline stage's stranded waiter."""
    _assert_stranded_pipeline_wait(
        asyncio.run(
            asyncio.wait_for(
                _run_stranded_pipeline_wait(monkeypatch),
                timeout=0.5,
            )
        )
    )
