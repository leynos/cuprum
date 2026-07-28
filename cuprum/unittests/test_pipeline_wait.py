"""State-machine tests for pipeline completion ordering (`_pipeline_wait`).

`_PipelineWaitState.record_completion` is the pure transition behind
`_process_completed_task`: given a completed stage index, its exit code, and an
injected end timestamp, it stamps the timing slot, latches the first non-zero
exit — in completion order — as ``failure_index``, and reports whether the
remaining downstream stages must be terminated. The Hypothesis
``RuleBasedStateMachine`` below drives randomized completion orders and pins
first-failure semantics, timing-slot population, and the termination decision;
example tests pin the boundary cases.
"""

from __future__ import annotations

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

from cuprum._pipeline_wait import _PipelineWaitState

if typ.TYPE_CHECKING:
    from hypothesis.strategies import DataObject


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
        assert self._state is not None
        idx = data.draw(st.sampled_from(self._pending), label="completed_idx")
        exit_code = data.draw(st.integers(min_value=-3, max_value=3), label="exit_code")
        self._clock += data.draw(
            st.floats(min_value=0.1, max_value=5.0),
            label="elapsed",
        )
        ended_at = self._clock

        needs_termination = self._state.record_completion(
            idx,
            exit_code,
            ended_at=ended_at,
        )
        self._pending.remove(idx)

        # Timing slot population and the exit code land in the right slots.
        assert self._state.exit_codes[idx] == exit_code
        assert self._state.ended_at[idx] == ended_at

        # First-failure semantics: ``failure_index`` latches the first non-zero
        # exit in completion order and never changes afterwards.
        is_first_failure = self._model_failure_index is None and exit_code != 0
        if is_first_failure:
            self._model_failure_index = idx
        assert self._state.failure_index == self._model_failure_index

        # Termination decision: terminate the remaining stages iff this is the
        # first failure and the failing stage is not the final one.
        expected = is_first_failure and idx != self._stage_count - 1
        assert needs_termination == expected

    @invariant()
    def failure_index_marks_a_recorded_nonzero_exit(self) -> None:
        """Once set, ``failure_index`` points at a recorded non-zero exit."""
        if self._state is None or self._state.failure_index is None:
            return
        code = self._state.exit_codes[self._state.failure_index]
        assert code is not None
        assert code != 0


TestPipelineCompletion = _PipelineCompletionMachine.TestCase
TestPipelineCompletion.settings = settings(
    max_examples=60,
    stateful_step_count=16,
    deadline=None,
)


class TestRecordCompletionExamples:
    """Pinned example scenarios for the completion transition."""

    def test_first_nonzero_exit_latches_failure_index(self) -> None:
        """The earliest-completed non-zero exit fixes ``failure_index``."""
        state = _make_wait_state(4)

        # Stage 2 succeeds, then stage 0 fails, then stage 3 fails: the first
        # *completed* failure (stage 0) must win, not the lowest index.
        assert state.record_completion(2, 0, ended_at=1.0) is False
        assert state.record_completion(0, 1, ended_at=2.0) is True
        assert state.record_completion(3, 7, ended_at=3.0) is False

        assert state.failure_index == 0
        assert state.exit_codes == [1, None, 0, 7]
        assert state.ended_at == [2.0, None, 1.0, 3.0]

    def test_final_stage_failure_requests_no_termination(self) -> None:
        """A failing final stage has nothing downstream to terminate."""
        state = _make_wait_state(3)

        assert state.record_completion(0, 0, ended_at=1.0) is False
        assert state.record_completion(1, 0, ended_at=2.0) is False
        # Stage 2 is the last stage: first failure, but no termination.
        assert state.record_completion(2, 5, ended_at=3.0) is False
        assert state.failure_index == 2

    def test_single_stage_failure_requests_no_termination(self) -> None:
        """A lone failing stage is also the final stage."""
        state = _make_wait_state(1)

        assert state.record_completion(0, 9, ended_at=1.5) is False
        assert state.failure_index == 0

    def test_all_success_records_no_failure(self) -> None:
        """An all-zero run leaves ``failure_index`` unset and never fails fast."""
        state = _make_wait_state(3)

        for idx in range(3):
            assert state.record_completion(idx, 0, ended_at=float(idx)) is False

        assert state.failure_index is None
        assert state.exit_codes == [0, 0, 0]
        assert state.ended_at == [0.0, 1.0, 2.0]

    def test_only_the_first_failure_requests_termination(self) -> None:
        """Later failures never re-trigger termination once one has failed."""
        state = _make_wait_state(4)

        assert state.record_completion(0, 1, ended_at=1.0) is True
        # A second, earlier-indexed non-final failure must not request another
        # termination: fail-fast fires exactly once.
        assert state.record_completion(1, 1, ended_at=2.0) is False
        assert state.failure_index == 0
