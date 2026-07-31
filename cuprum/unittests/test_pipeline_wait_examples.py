"""Pinned boundary cases for the completion transition.

`test_pipeline_wait_state_machine.py` generalises these over random completion
orders; the cases here fix the specific orderings whose behaviour is easiest to
get wrong — a lower-indexed stage failing *after* a higher-indexed one, and a
failing final stage that has nothing left to terminate.
"""

from __future__ import annotations

import typing as typ

from cuprum.unittests._pipeline_wait_support import make_wait_state

if typ.TYPE_CHECKING:
    from cuprum._pipeline_wait import _PipelineWaitState


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
        state = make_wait_state(4)

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
        state = make_wait_state(3)

        assert self._complete(state, 0, 0, 1.0) is False, "success never fails fast"
        assert self._complete(state, 1, 0, 2.0) is False, "success never fails fast"
        # Stage 2 is the last stage: first failure, but no termination.
        assert self._complete(state, 2, 5, 3.0) is False, (
            "a failing final stage has nothing left to terminate"
        )
        assert state.failure_index == 2, "the final stage still latches as the failure"

    def test_single_stage_failure_requests_no_termination(self) -> None:
        """A lone failing stage is also the final stage."""
        state = make_wait_state(1)

        assert self._complete(state, 0, 9, 1.5) is False, (
            "a single-stage pipeline has no other stage to terminate"
        )
        assert state.failure_index == 0, "the lone stage latches as the failure"

    def test_all_success_records_no_failure(self) -> None:
        """An all-zero run leaves ``failure_index`` unset and never fails fast."""
        state = make_wait_state(3)

        for idx in range(3):
            assert self._complete(state, idx, 0, float(idx)) is False, (
                f"successful stage {idx} must not request termination"
            )

        assert state.failure_index is None, "an all-success run records no failure"
        assert state.exit_codes == [0, 0, 0], "every stage records its zero exit"
        assert state.ended_at == [0.0, 1.0, 2.0], "every stage records its end time"

    def test_only_the_first_failure_requests_termination(self) -> None:
        """Later failures never re-trigger termination once one has failed."""
        state = make_wait_state(4)

        assert self._complete(state, 0, 1, 1.0) is True, (
            "the first failure requests termination"
        )
        # A second, non-final failure must not request another termination:
        # fail-fast fires exactly once.
        assert self._complete(state, 1, 1, 2.0) is False, (
            "fail-fast must fire exactly once per pipeline"
        )
        assert state.failure_index == 0, "the latched failure index never moves"
