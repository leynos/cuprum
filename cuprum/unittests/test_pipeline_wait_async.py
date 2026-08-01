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

    from cuprum._pipeline_wait import _PipelineWaitResult, _PipelineWaitState


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


class _SettledProcess:
    """A process stand-in whose ``wait`` returns immediately.

    ``_wait_for_pipeline`` only ever calls ``wait`` on the processes it is
    given, so this is enough to put every stage into a single
    ``asyncio.wait`` batch — the case where completion order runs out.
    """

    def __init__(self, exit_code: int) -> None:
        """Store the exit code this stand-in reports."""
        self._exit_code = exit_code

    async def wait(self) -> int:
        """Return the configured exit code without yielding to real I/O."""
        return self._exit_code


class TestSimultaneousCompletions:
    """Stages completing in one batch resolve by stage order, not set order.

    ``asyncio.wait`` returns an unordered set, so when several stages settle
    together there is no completion order left to observe. Reporting whichever
    the set happened to yield first would make ``failure_index`` — and the
    structured record naming the failing stage — vary between runs on
    identical input.
    """

    @staticmethod
    def _run(
        monkeypatch: pytest.MonkeyPatch,
        exit_codes: list[int],
    ) -> tuple[list[int], _PipelineWaitResult]:
        """Settle every stage at once, returning the processing order and result."""
        record_terminations(monkeypatch)
        pin_clock(monkeypatch, 12.5)

        processed: list[int] = []
        process_completed = _pipeline_wait._process_completed_task

        async def recording(
            task: asyncio.Task[int],
            state: _PipelineWaitState,
            processes: list[asyncio.subprocess.Process],
            cancel_grace: float,
        ) -> None:
            """Note which stage is being processed, then do the real work."""
            processed.append(state.task_to_index[task])
            await process_completed(task, state, processes, cancel_grace)

        monkeypatch.setattr(_pipeline_wait, "_process_completed_task", recording)

        # `_wait_for_pipeline` only ever calls `wait` on these, so the
        # stand-ins satisfy the part of the protocol that is actually used.
        processes = typ.cast(
            "list[asyncio.subprocess.Process]",
            [_SettledProcess(code) for code in exit_codes],
        )

        async def drive() -> _PipelineWaitResult:
            """Await the whole pipeline with every stage already settled."""
            return await _pipeline_wait._wait_for_pipeline(
                processes,
                pipe_tasks=[],
                cancel_grace=0.25,
                started_at=[0.0] * len(exit_codes),
            )

        return processed, asyncio.run(drive())

    def test_a_batch_is_processed_in_stage_order(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Every stage in one batch is handled lowest index first."""
        processed, _result = self._run(monkeypatch, [1, 1, 1, 1, 1, 1, 1, 1])

        assert processed == sorted(processed), (
            f"a batch must be processed in stage order, found {processed!r}"
        )
        assert processed == list(range(8)), (
            f"every stage must be processed exactly once, found {processed!r}"
        )

    def test_the_earliest_failing_stage_latches(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The upstream failure is reported, not whichever the set yielded.

        Stages 2 and 5 both fail in the same batch. Stage 2 is upstream, so it
        is the one that caused stage 5's failure and the one an operator needs
        named.
        """
        _processed, result = self._run(monkeypatch, [0, 0, 3, 0, 0, 7, 0])

        assert result.failure_index == 2, (
            f"the earliest failing stage must latch, found {result.failure_index!r}"
        )
