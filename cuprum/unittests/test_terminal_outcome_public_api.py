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


# The slot ``terminal_outcome`` occupied when it was published, and therefore
# the slot every released caller's positional argument still binds to. A field
# appended after it leaves this unchanged, which is why new fields may be
# added freely; an insertion ahead of it moves the index and breaks callers.
_TERMINAL_OUTCOME_INDEX = 28


def test_terminal_outcome_keeps_its_positional_slot() -> None:
    """The category's slot survives the fields appended after it.

    ``terminal_outcome`` was appended when it was added, and the assertion here
    used to say "last". "Last" was never the property that mattered: the
    declaration is a public, non-``kw_only`` dataclass, so what a positional
    caller needs is that the field stays at the index it was published at and
    that everything added later goes *after* it. Pinning the trailing position
    instead made every subsequent addition look like a regression here, which
    is a false signal rather than a caught defect.

    The index is pinned exactly rather than as "after ``exec_id``". An ordering
    between two fields is satisfied by any distance between them, so a field
    inserted between ``exec_id`` and ``terminal_outcome`` would shift every
    later positional argument by one and still pass an ordering assertion.
    ``_TERMINAL_OUTCOME_INDEX`` is what a released caller actually binds to.
    """
    fields = [field.name for field in dc.fields(c.ExecEvent)]

    assert "terminal_outcome" in fields, (
        f"the settled category must remain a declared field; got {fields}"
    )

    # Only ``kw_only`` fields are excluded, because only they are absent from
    # a positional call. Measuring ``dc.fields()`` instead would count a
    # keyword-only field declared ahead of ``terminal_outcome`` as if it had
    # moved the slot a caller binds to, which is a false regression: the field
    # is not in the positional sequence at all.
    positional = [field.name for field in dc.fields(c.ExecEvent) if not field.kw_only]
    assert positional.index("terminal_outcome") == _TERMINAL_OUTCOME_INDEX, (
        "terminal_outcome must keep the slot its positional callers bind to; "
        f"expected index {_TERMINAL_OUTCOME_INDEX}, got "
        f"{positional.index('terminal_outcome')} of {len(positional)} positional "
        f"fields: {positional}"
    )
