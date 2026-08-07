"""Hypothesis state machine over randomized pipeline completion orders.

`_PipelineWaitState.record_completion` latches the first non-zero exit **in
completion order**, and `should_terminate_others` reports whether that
completion should stop every other still-running stage. This machine drives
random completion orders through both and cross-checks them against an
independent model, so the ordering rules are pinned without processes or a
clock. The pinned boundary cases live in `test_pipeline_wait_examples.py`.
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

from cuprum.unittests._pipeline_wait_support import make_wait_state

if typ.TYPE_CHECKING:
    from hypothesis.strategies import DataObject

    from cuprum._pipeline_wait import _PipelineWaitState


class _PipelineCompletionMachine(RuleBasedStateMachine):
    """Drive random stage-completion orders through the pure transition."""

    def __init__(self) -> None:
        """Start with no pipeline; ``setup`` initializes one per example."""
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
        self._state = make_wait_state(stage_count)

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
