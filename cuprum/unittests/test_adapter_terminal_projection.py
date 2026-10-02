"""Projection tests for settled execution outcomes across telemetry adapters."""

from __future__ import annotations

import pytest

from cuprum.adapters._support import _event_common_fields
from cuprum.adapters.logging_adapter import _build_extra
from cuprum.adapters.tracing_adapter import TracingHook
from cuprum.events import TerminalOutcome
from cuprum.unittests._adapter_test_support import _make_exec_event


@pytest.mark.parametrize("outcome", list(TerminalOutcome))
def test_settled_outcome_projects_as_a_string(outcome: TerminalOutcome) -> None:
    """The terminal category remains available in canonical and adapter fields."""
    event = _make_exec_event(
        phase="settled",
        overrides={"terminal_outcome": outcome},
    )

    canonical_fields = dict(_event_common_fields(event, lambda field: field))
    logging_fields = _build_extra(event)
    tracing_fields = TracingHook._build_attributes(event)

    assert canonical_fields["terminal_outcome"] == str(outcome), (
        "the canonical projection must render the category's stable value"
    )
    assert logging_fields["cuprum_terminal_outcome"] == str(outcome), (
        "structured logging must preserve the terminal category"
    )
    assert tracing_fields["cuprum.terminal_outcome"] == str(outcome), (
        "tracing must preserve the terminal category"
    )
