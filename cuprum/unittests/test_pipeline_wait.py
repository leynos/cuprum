"""Unit tests for the internal ``_wait_for_pipeline`` fail-fast behaviour."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import pytest

from cuprum._testing import (
    _PipelineWaitResult,
    _wait_for_pipeline,
)


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
        started_at=[0.0, 0.0, 0.0],
    )

    return processes[0], processes[1], processes[2], result


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
                ready_stages=frozenset([0, 1, 2]),
                expected_failure_index=2,
                expected_exit_codes=(0, 0, 5),
                terminated_stages=frozenset(),
            ),
            id="last-stage-failure-no-termination",
        ),
    ],
)
def test_wait_for_pipeline_fail_fast_scenarios(
    scenario: _FailFastScenario,
) -> None:
    """Validate fail-fast termination behaviour across different failure scenarios.

    Tests that:
    - Early and middle stage failures terminate all other stages
    - Final stage failures record failure index without terminating others
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
