"""Observability contracts for pipeline-wait fail-fast completion."""

from __future__ import annotations

import logging
import typing as typ

from cuprum import _pipeline_wait
from cuprum.adapters.logging_adapter import structured_logging_hook
from cuprum.unittests._pipeline_wait_support import (
    apply_completions,
    make_stage_observations,
    make_wait_state,
    pin_clock,
    record_actions,
    record_terminations,
)

if typ.TYPE_CHECKING:
    import pytest


class _CompletionRecordFields(typ.TypedDict):
    """Structured extras present on every pipeline-wait completion record."""

    cuprum_stage_index: int
    cuprum_stage_count: int
    cuprum_exit_code: int
    cuprum_duration_s: float
    cuprum_exec_id: str


class _TerminationOutcomeFields(_CompletionRecordFields):
    """Completion record extras added after termination finishes."""

    cuprum_terminated_stage_count: int
    cuprum_termination_duration_s: float


class TestCompletionObservability:
    """Verify the records a fail-fast teardown publishes."""

    def test_fail_fast_reports_ordered_termination_records(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A real teardown reports its latch, request, and outcome in order."""
        logger = logging.getLogger(_pipeline_wait.__name__)
        observations = make_stage_observations(
            3,
            (structured_logging_hook(logger=logger),),
        )
        record_terminations(monkeypatch, terminated_count=2)
        pin_clock(monkeypatch, 12.5)
        state = make_wait_state(3, observations=observations)

        with caplog.at_level(logging.WARNING, logger=_pipeline_wait.__name__):
            apply_completions(state, [(0, 4)])

        action_records = tuple(
            record for record in caplog.records if "cuprum_action" in vars(record)
        )
        assert record_actions(action_records) == [
            "pipeline_stage_first_failure",
            "pipeline_fail_fast_termination",
            "pipeline_fail_fast_terminated",
        ], f"unexpected pipeline-wait records: {action_records!r}"
        expected_exec_id = str(observations[0].exec_id)
        for record in action_records:
            fields = typ.cast("_CompletionRecordFields", vars(record))
            assert (
                fields["cuprum_stage_index"],
                fields["cuprum_stage_count"],
                fields["cuprum_exit_code"],
                fields["cuprum_duration_s"],
                fields["cuprum_exec_id"],
            ) == (0, 3, 4, 12.5, expected_exec_id)
        outcome = action_records[-1]
        outcome_fields = typ.cast("_TerminationOutcomeFields", vars(outcome))
        assert outcome_fields["cuprum_terminated_stage_count"] == 2
        assert outcome_fields["cuprum_termination_duration_s"] >= 0.0
