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
    """Appending later fields must not displace an earlier appended field.

    ``ExecEvent`` is a public, non-``kw_only`` dataclass, so every optional
    field's slot is part of the contract. ``terminal_outcome`` was appended
    directly after ``env_mode``, which was the last field then; new fields
    therefore go *after* it rather than before, and the durable invariant is
    that adjacency rather than being the final field. Asserting on the tail
    would fail for every legitimate append — and would push a future field in
    ahead of ``terminal_outcome``, moving the slot this test exists to fix.
    """
    fields = [field.name for field in dc.fields(c.ExecEvent)]

    assert fields.index("terminal_outcome") == fields.index("env_mode") + 1, (
        "terminal_outcome must directly follow env_mode, the field it was "
        "appended after, so that appending error_category leaves every "
        f"existing positional slot unchanged, got {fields}"
    )
