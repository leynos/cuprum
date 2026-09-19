"""Tests for the shared presentation-sink session lifecycle.

``cuprum._sink_lifecycle`` owns the bracket every run hangs its adapter
session on: opened once before the work starts and closed exactly once on
each terminal path. These tests pin the bracket's take-once semantics and the
outcomes that reach an adapter at the end of a real run.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum._sink_lifecycle import (
    _close_sink_session,
    _open_sink_session,
    _outcome_for_error,
    _run_label,
    _SinkBracket,
)
from cuprum.sh import RunOutputOptions, TimeoutExpired
from cuprum.sinks import (
    SessionOutcome,
    SessionStart,
    TerminalOutcome,
)
from cuprum.unittests._sink_test_support import RecordingSession, RecordingSink
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd


def _python_builder() -> cabc.Callable[..., SafeCmd]:
    """Build a SafeCmd factory for the current interpreter."""
    return build_python_builder()


def _recorded_outcome(adapter: RecordingSink) -> SessionOutcome:
    """Return the outcome recorded by the adapter's most recent session."""
    return adapter.recorded_outcome


# ---------------------------------------------------------------------------
# Run-level sink session lifecycle
# ---------------------------------------------------------------------------


def test_run_output_options_sink_defaults_to_none() -> None:
    """RunOutputOptions leaves the sink unset so runs stay unchanged by default."""
    options = RunOutputOptions()

    assert options.sink is None, (
        f"a default RunOutputOptions must leave the sink unset; got {options.sink!r}"
    )


def test_no_sink_keeps_plain_destinations() -> None:
    """A run without a sink captures output exactly as before."""
    command = _python_builder()("-c", "print('plain')")

    result = command.run_sync()

    assert result.ok is True, f"a plain run must succeed; got {result!r}"
    assert result.stdout == "plain\n", (
        f"a run without a sink must capture normally; got {result.stdout!r}"
    )


def test_sink_declining_activation_is_a_no_op() -> None:
    """A sink that returns None from open_session leaves the run unchanged."""
    adapter = RecordingSink(decline=True)
    command = _python_builder()("-c", "print('declined')")

    result = command.run_sync(output=RunOutputOptions(sink=adapter))

    assert result.ok is True, f"a declined sink must not affect the run; got {result!r}"
    assert result.stdout == "declined\n", (
        f"a declined sink must leave capture unchanged; got {result.stdout!r}"
    )
    assert adapter.opened == 1, (
        f"a declining adapter is still consulted once; opened={adapter.opened}"
    )
    assert adapter.started_with is not None, (
        "a declining adapter must still receive the session start"
    )
    # Compare against the command's own argv and the runner's own label
    # derivation so the assertion holds for any interpreter spelling
    # (``python3.12``, ``/usr/bin/python``, …) rather than a fixed name.
    assert adapter.started_with.argv == command.argv_with_program, (
        f"the session start must carry the command's argv; "
        f"got {adapter.started_with.argv!r}"
    )
    assert adapter.started_with.label == _run_label(command), (
        f"the session start must carry the derived label; "
        f"got {adapter.started_with.label!r}"
    )


def test_sink_session_opens_once_and_closes_on_success() -> None:
    """The sink session is opened once per run and closed with exit_zero."""
    adapter = RecordingSink()
    command = _python_builder()("-c", "print('framed')")

    result = command.run_sync(output=RunOutputOptions(sink=adapter))

    assert result.ok is True, f"the framed run must succeed; got {result!r}"
    assert adapter.opened == 1, (
        f"one run must open exactly one sink session; opened={adapter.opened}"
    )
    assert adapter.started_with is not None, "a run with a sink must open a session"
    outcome = _recorded_outcome(adapter)
    assert outcome.outcome == TerminalOutcome.EXIT_ZERO, (
        f"a successful run must close with {TerminalOutcome.EXIT_ZERO}; "
        f"got {outcome.outcome}"
    )
    assert outcome.exit_code == 0, (
        f"a successful run must report exit code 0; got {outcome.exit_code}"
    )


def test_sink_session_closes_on_nonzero_exit() -> None:
    """A failing command still closes its session with exit_nonzero."""
    adapter = RecordingSink()
    command = _python_builder()("-c", "raise SystemExit(3)")

    result = command.run_sync(output=RunOutputOptions(sink=adapter))

    assert result.ok is False, f"a failing run must not report success; got {result!r}"
    assert result.exit_code == 3, (
        f"the run must report the child's exit code 3; got {result.exit_code}"
    )
    outcome = _recorded_outcome(adapter)
    assert outcome.outcome == TerminalOutcome.EXIT_NONZERO, (
        f"a failing run must close with {TerminalOutcome.EXIT_NONZERO}; "
        f"got {outcome.outcome}"
    )
    assert outcome.exit_code == 3, (
        f"the recorded exit code must be the child's 3; got {outcome.exit_code}"
    )


def test_sink_session_closes_on_timeout() -> None:
    """A timed-out command still closes its session with the timeout outcome."""
    adapter = RecordingSink()
    command = _python_builder()("-c", "import time; time.sleep(2)")

    with pytest.raises(TimeoutExpired, match=r"timed out"):
        command.run_sync(
            output=RunOutputOptions(sink=adapter),
            timeout=0.1,
        )

    outcome = _recorded_outcome(adapter)
    assert outcome.outcome == TerminalOutcome.TIMEOUT, (
        f"a timed-out run must close with {TerminalOutcome.TIMEOUT}; "
        f"got {outcome.outcome}"
    )
    assert outcome.exit_code is None, (
        f"a timeout must carry no exit code; got {outcome.exit_code}"
    )


class _TitledRecordingSink(RecordingSink):
    """A sink carrying an adapter-private ``title`` the protocol never declares."""

    def __init__(self, title: str) -> None:
        """Record the adapter's own title configuration."""
        super().__init__()
        self.title = title


def test_sink_session_label_ignores_undeclared_adapter_attributes() -> None:
    """The shared lifecycle reads only the declared OutputSink protocol.

    ``title`` is ``GitHubActionsSink``'s own configuration, not part of the
    protocol. An adapter that happens to carry one must not have it reach
    ``SessionStart.label``, or the shared layer would be depending on a
    concrete adapter's private attribute. The adapter applies its own title
    inside ``open_session`` instead.
    """
    adapter = _TitledRecordingSink("Custom title")
    command = _python_builder()("-c", "print('titled')")

    result = command.run_sync(output=RunOutputOptions(sink=adapter))

    assert result.ok is True, f"the titled run must succeed; got {result!r}"
    assert adapter.started_with is not None, "a run with a sink must open a session"
    assert adapter.started_with.label == _run_label(command), (
        f"an adapter-private title must not reach the shared label; "
        f"got {adapter.started_with.label!r}"
    )


def test_sink_session_label_omits_argv() -> None:
    """The derived label never contains arguments, only the program name."""
    adapter = RecordingSink()
    secret = "s3cret-token-9f2aXq7"  # ruff: ignore[hardcoded-password-string] - synthetic test token, never a real credential.
    command = _python_builder()("-c", f"print('{secret}')")

    command.run_sync(output=RunOutputOptions(sink=adapter))

    assert adapter.started_with is not None, "a run with a sink must open a session"
    assert secret not in adapter.started_with.label, (
        f"the label must never republish argv; got {adapter.started_with.label!r}"
    )


# ---------------------------------------------------------------------------
# The take-once session bracket
# ---------------------------------------------------------------------------


def test_bracket_opens_once_and_closes_once() -> None:
    """The bracket returns the adapter's session and releases it on close."""
    session = RecordingSession()
    adapter = RecordingSink(session_factory=lambda: session)
    start = SessionStart(label="project: program", argv=("python", "-c"))

    bracket = _SinkBracket.open(adapter, start)

    assert bracket.session is session, (
        f"the bracket must own the adapter's session; got {bracket.session!r}"
    )
    assert adapter.started_with == start, (
        f"the adapter must receive the session start it was opened with; "
        f"got {adapter.started_with!r}"
    )
    assert adapter.opened == 1, (
        f"opening a bracket consults the adapter once; opened={adapter.opened}"
    )
    outcome = SessionOutcome(TerminalOutcome.EXIT_ZERO, exit_code=0)
    bracket.close(outcome=outcome)
    assert session.closed_with == outcome, (
        f"closing must reach the adapter with the given outcome; "
        f"got {session.closed_with!r}"
    )
    assert bracket.session is None, "closing must release the session"


def test_bracket_ignores_a_later_close() -> None:
    """Only the first close reaches the adapter, so its outcome stands."""
    session = RecordingSession()
    bracket = _SinkBracket.open(
        RecordingSink(session_factory=lambda: session),
        SessionStart(label="project: program", argv=()),
    )

    first = SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=3)
    bracket.close(outcome=first)
    bracket.close(outcome=SessionOutcome(TerminalOutcome.TIMEOUT, detail="timeout"))

    assert session.closed_with == first, (
        f"a second close must not displace the first outcome; "
        f"got {session.closed_with!r}, expected {first!r}"
    )


def test_bracket_without_a_sink_is_empty_and_closes_silently() -> None:
    """A bracket opened over no sink owns nothing and closes nothing."""
    bracket = _SinkBracket.open(None, SessionStart(label="project: program", argv=()))

    assert bracket.session is None, (
        f"a bracket over no sink must be empty; got {bracket.session!r}"
    )
    bracket.close(outcome=SessionOutcome(TerminalOutcome.ERROR))

    assert bracket.session is None, (
        f"closing an empty bracket must stay empty; got {bracket.session!r}"
    )


def test_bracket_over_a_declining_adapter_is_empty() -> None:
    """An adapter that declines activation leaves an empty bracket."""
    adapter = RecordingSink(decline=True)

    bracket = _SinkBracket.open(adapter, SessionStart(label="l", argv=()))

    assert bracket.session is None, (
        f"a declining adapter must leave the bracket empty; got {bracket.session!r}"
    )
    assert adapter.opened == 1, (
        f"a declining adapter is still consulted once; opened={adapter.opened}"
    )


def test_open_sink_session_returns_the_adapters_session() -> None:
    """Opening a sink hands back the session the adapter returned."""
    session = RecordingSession()
    adapter = RecordingSink(session_factory=lambda: session)

    opened = _open_sink_session(adapter, SessionStart(label="l", argv=("p",)))

    assert opened is session, (
        f"opening must hand back the adapter's own session; got {opened!r}"
    )


def test_open_sink_session_without_a_sink_is_none() -> None:
    """A run with no sink opens nothing to close later."""
    opened = _open_sink_session(None, SessionStart(label="l", argv=()))

    assert opened is None, f"a run with no sink must open nothing; got {opened!r}"


def test_close_sink_session_without_a_session_is_a_no_op() -> None:
    """Closing a run that never opened a session does nothing."""
    _close_sink_session(None, outcome=SessionOutcome(TerminalOutcome.ERROR))


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            TimeoutExpired(cmd=("python", "-c"), timeout=0.5),
            SessionOutcome(
                outcome=TerminalOutcome.TIMEOUT,
                exit_code=None,
                detail="timeout",
            ),
            id="timeout",
        ),
        pytest.param(
            asyncio.CancelledError(),
            SessionOutcome(outcome=TerminalOutcome.CANCELLED),
            id="cancelled",
        ),
        pytest.param(
            RuntimeError("boom"),
            SessionOutcome(outcome=TerminalOutcome.ERROR),
            id="error",
        ),
    ],
)
def test_outcome_for_error_maps_each_terminal_category(
    error: BaseException,
    expected: SessionOutcome,
) -> None:
    """Only the bounded categorical outcomes reach the adapter."""
    mapped = _outcome_for_error(error)
    assert mapped == expected, (
        f"{type(error).__name__} must map to {expected!r}; got {mapped!r}"
    )
