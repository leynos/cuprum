"""Public shape and shared taxonomy for terminal execution outcomes."""

from __future__ import annotations

import dataclasses as dc

import cuprum as c
from cuprum.events import TerminalOutcome as EventTerminalOutcome
from cuprum.sinks import TerminalOutcome as SinkTerminalOutcome


def test_terminal_outcome_is_shared_by_event_and_sink_channels() -> None:
    """The event and presentation channels use one closed outcome taxonomy."""
    assert EventTerminalOutcome is SinkTerminalOutcome, (
        "sink outcomes must alias the events-layer definition"
    )
    assert [outcome.value for outcome in EventTerminalOutcome] == [
        "exit_zero",
        "exit_nonzero",
        "timeout",
        "cancelled",
        "error",
    ]


def test_terminal_outcome_is_appended_to_exec_event_fields() -> None:
    """Adding the category preserves all existing positional fields."""
    fields = [field.name for field in dc.fields(c.ExecEvent)]

    assert fields[-1] == "terminal_outcome", (
        "terminal_outcome must be appended after existing positional fields, "
        f"got {fields}"
    )
