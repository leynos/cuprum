"""State-machine tests for pipeline completion ordering (`_pipeline_wait`).

Completion handling splits into a command and a query.
`_PipelineWaitState.record_completion` stamps the completed stage's exit code
and injected end timestamp and latches the first non-zero exit — in completion
order — as ``failure_index``. `should_terminate_others` then reports whether
that completion should stop every other still-running stage. The Hypothesis
``RuleBasedStateMachine`` below drives randomized completion orders and pins
first-failure semantics, timing-slot population, and the termination decision;
example tests pin the boundary cases, and `TestProcessCompletedTask` covers the
async wiring in `_process_completed_task` that joins the two.
"""

from __future__ import annotations

import asyncio
import typing as typ

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from cuprum import _pipeline_wait
from cuprum._pipeline_wait import _PipelineWaitState

if typ.TYPE_CHECKING:
    import pytest
    from hypothesis.strategies import DataObject


async def _immediate(exit_code: int) -> int:
    """Return ``exit_code`` after a yield, standing in for ``Process.wait()``."""
    await asyncio.sleep(0)
    return exit_code


def _make_wait_state(stage_count: int) -> _PipelineWaitState:
    """Build a bare wait state for ``stage_count`` stages.

    The pure ``record_completion`` transition touches only the exit-code,
    timing, and failure-index bookkeeping, so the task fields are left empty:
    no event loop or subprocess is required to exercise completion ordering.
    """
    return _PipelineWaitState(
        wait_tasks=[],
        task_to_index={},
        exit_codes=[None] * stage_count,
        started_at=[0.0] * stage_count,
        ended_at=[None] * stage_count,
    )


class _PipelineCompletionMachine(RuleBasedStateMachine):
    """Drive random stage-completion orders through the pure transition."""

    def __init__(self) -> None:
        """Start with no pipeline; ``setup`` initialises one per example."""
        super().__init__()
        self._state: _PipelineWaitState | None = None
        self._stage_count = 0
        self._pending: list[int] = []
        self._model_failure_index: int | None = None
        self._clock = 0.0

    def _begin_pipeline(self, stage_count: int) -> None:
        """Reset the model and state to a fresh pipeline of ``stage_count``."""
        self._stage_count = stage_count
        self._pending = list(range(stage_count))
        self._model_failure_index = None
        self._clock = 0.0
        self._state = _make_wait_state(stage_count)

    @initialize(stage_count=st.integers(min_value=1, max_value=8))
    def setup(self, stage_count: int) -> None:
        """Create a fresh wait state with ``stage_count`` un-started stages."""
        self._begin_pipeline(stage_count)

    @precondition(lambda self: not self._pending)
    @rule(stage_count=st.integers(min_value=1, max_value=8))
    def restart(self, stage_count: int) -> None:
        """Once every stage has completed, begin a fresh pipeline.

        This keeps the machine making progress after a pipeline drains and
        lets a single run explore several independent completion orders.
        """
        self._begin_pipeline(stage_count)

    @precondition(lambda self: bool(self._pending))
    @rule(data=st.data())
    def complete_stage(self, data: DataObject) -> None:
        """Complete one still-pending stage and verify the transition."""
        assert self._state is not None, "setup must run before any completion"
        idx = data.draw(st.sampled_from(self._pending), label="completed_idx")
        exit_code = data.draw(st.integers(min_value=-3, max_value=3), label="exit_code")
        self._clock += data.draw(
            st.floats(min_value=0.1, max_value=5.0),
            label="elapsed",
        )
        ended_at = self._clock

        self._state.record_completion(idx, exit_code, ended_at=ended_at)
        needs_termination = self._state.should_terminate_others(idx)
        self._pending.remove(idx)

        # Timing slot population and the exit code land in the right slots.
        assert self._state.exit_codes[idx] == exit_code, (
            "the completed stage's exit code must land in its own slot"
        )
        assert self._state.ended_at[idx] == ended_at, (
            "the injected end time must land in the completed stage's slot"
        )

        # First-failure semantics: ``failure_index`` latches the first non-zero
        # exit in completion order and never changes afterwards.
        is_first_failure = self._model_failure_index is None and exit_code != 0
        if is_first_failure:
            self._model_failure_index = idx
        assert self._state.failure_index == self._model_failure_index, (
            "failure_index must latch the first non-zero exit in completion order"
        )

        # Termination decision: fail fast iff this is the first failure and the
        # failing stage is not the final one.
        expected = is_first_failure and idx != self._stage_count - 1
        assert needs_termination == expected, (
            "should_terminate_others must hold only for a non-final first failure"
        )

        # The query is pure: asking again changes nothing and answers the same.
        assert self._state.should_terminate_others(idx) == needs_termination, (
            "should_terminate_others must be repeatable without side effects"
        )

    @invariant()
    def failure_index_marks_a_recorded_nonzero_exit(self) -> None:
        """Once set, ``failure_index`` points at a recorded non-zero exit."""
        if self._state is None or self._state.failure_index is None:
            return
        code = self._state.exit_codes[self._state.failure_index]
        assert code is not None, "failure_index must point at a recorded exit code"
        assert code != 0, "failure_index must point at a non-zero exit code"


TestPipelineCompletion = _PipelineCompletionMachine.TestCase
TestPipelineCompletion.settings = settings(
    max_examples=60,
    stateful_step_count=16,
    deadline=None,
)


class TestRecordCompletionExamples:
    """Pinned example scenarios for the completion transition."""

    @staticmethod
    def _complete(
        state: _PipelineWaitState,
        idx: int,
        exit_code: int,
        ended_at: float,
    ) -> bool:
        """Record a completion and return the fail-fast decision for it."""
        state.record_completion(idx, exit_code, ended_at=ended_at)
        return state.should_terminate_others(idx)

    def test_first_nonzero_exit_latches_failure_index(self) -> None:
        """The earliest-completed non-zero exit fixes ``failure_index``."""
        state = _make_wait_state(4)

        # Stage 2 succeeds, then stage 0 fails, then stage 3 fails: the first
        # *completed* failure (stage 0) must win, not the lowest index.
        assert self._complete(state, 2, 0, 1.0) is False, (
            "a successful stage must not request termination"
        )
        assert self._complete(state, 0, 1, 2.0) is True, (
            "the first completed failure must request termination"
        )
        assert self._complete(state, 3, 7, 3.0) is False, (
            "a later failure must not re-request termination"
        )

        assert state.failure_index == 0, "the first completed failure must latch"
        assert state.exit_codes == [1, None, 0, 7], "exit codes land per stage index"
        assert state.ended_at == [2.0, None, 1.0, 3.0], "end times land per index"

    def test_final_stage_failure_requests_no_termination(self) -> None:
        """A failing final stage has no other running stage to stop."""
        state = _make_wait_state(3)

        assert self._complete(state, 0, 0, 1.0) is False, "success never fails fast"
        assert self._complete(state, 1, 0, 2.0) is False, "success never fails fast"
        # Stage 2 is the last stage: first failure, but no termination.
        assert self._complete(state, 2, 5, 3.0) is False, (
            "a failing final stage has nothing left to terminate"
        )
        assert state.failure_index == 2, "the final stage still latches as the failure"

    def test_single_stage_failure_requests_no_termination(self) -> None:
        """A lone failing stage is also the final stage."""
        state = _make_wait_state(1)

        assert self._complete(state, 0, 9, 1.5) is False, (
            "a single-stage pipeline has no other stage to terminate"
        )
        assert state.failure_index == 0, "the lone stage latches as the failure"

    def test_all_success_records_no_failure(self) -> None:
        """An all-zero run leaves ``failure_index`` unset and never fails fast."""
        state = _make_wait_state(3)

        for idx in range(3):
            assert self._complete(state, idx, 0, float(idx)) is False, (
                f"successful stage {idx} must not request termination"
            )

        assert state.failure_index is None, "an all-success run records no failure"
        assert state.exit_codes == [0, 0, 0], "every stage records its zero exit"
        assert state.ended_at == [0.0, 1.0, 2.0], "every stage records its end time"

    def test_only_the_first_failure_requests_termination(self) -> None:
        """Later failures never re-trigger termination once one has failed."""
        state = _make_wait_state(4)

        assert self._complete(state, 0, 1, 1.0) is True, (
            "the first failure requests termination"
        )
        # A second, non-final failure must not request another termination:
        # fail-fast fires exactly once.
        assert self._complete(state, 1, 1, 2.0) is False, (
            "fail-fast must fire exactly once per pipeline"
        )
        assert state.failure_index == 0, "the latched failure index never moves"


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
        terminations: list[tuple[int, float]] = []

        async def fake_terminate(
            processes: object,
            wait_tasks: object,
            failure_index: int,
            *,
            cancel_grace: float,
        ) -> None:
            """Record the termination request instead of signalling processes."""
            del processes, wait_tasks
            await asyncio.sleep(0)
            terminations.append((failure_index, cancel_grace))

        monkeypatch.setattr(
            _pipeline_wait,
            "_terminate_pipeline_remaining_stages",
            fake_terminate,
        )
        monkeypatch.setattr(_pipeline_wait.time, "perf_counter", lambda: 12.5)

        state = _make_wait_state(stage_count)

        async def drive() -> None:
            """Build a settled wait task for the stage and process it."""
            task = asyncio.create_task(_immediate(exit_code))
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
