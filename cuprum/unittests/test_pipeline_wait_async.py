"""Async-boundary tests for `_process_completed_task`.

The command and the query are pure and covered elsewhere. This module covers
the wiring that joins them: reading the clock, applying the command, and
awaiting fail-fast termination when the query says so.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum import _pipeline_wait
from cuprum.unittests._pipeline_wait_support import (
    immediate,
    make_wait_state,
    pin_clock,
    record_terminations,
)

if typ.TYPE_CHECKING:
    import pytest

    from cuprum._pipeline_wait import _PipelineWaitState


class TestProcessCompletedTask:
    """Async integration tests for the ``_process_completed_task`` boundary.

    The pure transition is covered above; these cover the wiring around it —
    that the completed task's index and result reach ``record_completion``, that
    the clock is read from ``time.perf_counter``, and that termination is
    invoked exactly when ``should_terminate_others`` says so, with the failing
    stage's index forwarded.
    """

    @staticmethod
    def _run_completion(
        monkeypatch: pytest.MonkeyPatch,
        *,
        stage_count: int,
        completed_idx: int,
        exit_code: int,
    ) -> tuple[_PipelineWaitState, list[tuple[int, float]]]:
        """Drive one completed wait task, recording any termination call."""
        terminations = record_terminations(monkeypatch)
        pin_clock(monkeypatch, 12.5)

        state = make_wait_state(stage_count)

        async def drive() -> None:
            """Build a settled wait task for the stage and process it."""
            task = asyncio.create_task(immediate(exit_code))
            await task
            state.wait_tasks = [task]
            state.task_to_index = {task: completed_idx}
            await _pipeline_wait._process_completed_task(
                task,
                state,
                [],
                0.25,
            )

        asyncio.run(drive())
        return state, terminations

    def test_records_the_completion_and_terminates_on_first_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-final first failure records state and terminates other stages."""
        state, terminations = self._run_completion(
            monkeypatch,
            stage_count=3,
            completed_idx=0,
            exit_code=4,
        )

        assert state.exit_codes[0] == 4, "the task result must reach record_completion"
        assert state.ended_at[0] == 12.5, (
            "the injected perf_counter reading must be stamped as the end time"
        )
        assert state.failure_index == 0, "the failing stage must latch"
        assert terminations == [(0, 0.25)], (
            "termination must run once with the failing index and cancel grace"
        )

    def test_successful_stage_records_without_terminating(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A zero exit is recorded and never triggers termination."""
        state, terminations = self._run_completion(
            monkeypatch,
            stage_count=3,
            completed_idx=1,
            exit_code=0,
        )

        assert state.exit_codes[1] == 0, "the zero exit must be recorded"
        assert state.ended_at[1] == 12.5, "the end time must still be stamped"
        assert state.failure_index is None, "a success must not latch a failure"
        assert terminations == [], "a successful stage must not terminate others"

    def test_failing_final_stage_records_without_terminating(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing final stage latches but has nothing left to terminate."""
        state, terminations = self._run_completion(
            monkeypatch,
            stage_count=3,
            completed_idx=2,
            exit_code=1,
        )

        assert state.failure_index == 2, "the final stage must still latch"
        assert terminations == [], "a failing final stage must not request termination"
