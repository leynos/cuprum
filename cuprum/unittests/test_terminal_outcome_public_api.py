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


def test_terminal_outcome_keeps_its_positional_slot() -> None:
    """The category's slot survives the fields appended after it.

    ``terminal_outcome`` was appended when it was added, and the assertion here
    used to say "last". "Last" was never the property that mattered: the
    declaration is a public, non-``kw_only`` dataclass, so what a positional
    caller needs is that the field stays where it was put and that everything
    added later goes *after* it. Pinning the trailing position instead made
    every subsequent addition look like a regression here, which is a false
    signal rather than a caught defect.
    """
    fields = [field.name for field in dc.fields(c.ExecEvent)]

    assert "terminal_outcome" in fields, (
        f"the settled category must remain a declared field; got {fields}"
    )
    assert fields.index("terminal_outcome") > fields.index("exec_id"), (
        "terminal_outcome must stay after exec_id, or an existing positional "
        f"caller would bind the category to the correlation token; got {fields}"
    )
