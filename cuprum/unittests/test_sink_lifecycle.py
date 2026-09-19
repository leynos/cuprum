"""Tests for the shared presentation-sink session lifecycle.

``cuprum._sink_lifecycle`` owns the bracket every run hangs its adapter
session on: opened once before the work starts and closed exactly once on
each terminal path. These tests pin the bracket's take-once semantics and the
outcomes that reach an adapter at the end of a real run.
"""

from __future__ import annotations

import asyncio
import io
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
    OutputSession,
    SessionOutcome,
    SessionStart,
    TerminalOutcome,
)
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd


def _python_builder() -> cabc.Callable[..., SafeCmd]:
    """Build a SafeCmd factory for the current interpreter."""
    return build_python_builder()


class _RecordingSession:
    """Minimal OutputSession recording writes and the close outcome."""

    def __init__(self) -> None:
        """Start with an in-memory log and no closed outcome."""
        self.log_io = io.StringIO()
        self.closed_with: SessionOutcome | None = None

    @property
    def log(self) -> typ.IO[str]:
        """The in-memory log destination."""
        return self.log_io

    def close(self, outcome: SessionOutcome) -> None:
        """Record the terminal outcome."""
        self.closed_with = outcome


class _RecordingSink:
    """Minimal OutputSink returning a recording session."""

    title: str | None

    def __init__(
        self,
        *,
        decline: bool = False,
        session_factory: cabc.Callable[[], _RecordingSession] = _RecordingSession,
    ) -> None:
        """Configure whether the adapter declines activation."""
        self.decline = decline
        self.title = None
        self.started_with: SessionStart | None = None
        self.opened = 0
        self.last_session: _RecordingSession | None = None
        self._session_factory = session_factory

    def open_session(self, start: SessionStart) -> OutputSession | None:
        """Record the start and return a fresh recording session."""
        self.started_with = start
        self.opened += 1
        if self.decline:
            return None
        self.last_session = self._session_factory()
        return self.last_session


def _recorded_outcome(adapter: _RecordingSink) -> SessionOutcome:
    """Return the outcome recorded by the adapter's most recent session."""
    session = adapter.last_session
    assert session is not None, "the run must have opened a sink session"
    assert session.closed_with is not None, (
        "every terminal path must close the sink session"
    )
    return session.closed_with


# ---------------------------------------------------------------------------
# Run-level sink session lifecycle
# ---------------------------------------------------------------------------


def test_run_output_options_sink_defaults_to_none() -> None:
    """RunOutputOptions leaves the sink unset so runs stay unchanged by default."""
    options = RunOutputOptions()

    assert options.sink is None


def test_no_sink_keeps_plain_destinations() -> None:
    """A run without a sink captures output exactly as before."""
    command = _python_builder()("-c", "print('plain')")

    result = command.run_sync()

    assert result.ok is True
    assert result.stdout == "plain\n"


def test_sink_declining_activation_is_a_no_op() -> None:
    """A sink that returns None from open_session leaves the run unchanged."""
    adapter = _RecordingSink(decline=True)
    command = _python_builder()("-c", "print('declined')")

    result = command.run_sync(output=RunOutputOptions(sink=adapter))

    assert result.ok is True
    assert result.stdout == "declined\n"
    assert adapter.opened == 1
    assert adapter.started_with is not None
    # Compare against the command's own argv and the runner's own label
    # derivation so the assertion holds for any interpreter spelling
    # (``python3.12``, ``/usr/bin/python``, …) rather than a fixed name.
    assert adapter.started_with.argv == command.argv_with_program
    assert adapter.started_with.label == _run_label(command, None)


def test_sink_session_opens_once_and_closes_on_success() -> None:
    """The sink session is opened once per run and closed with exit_zero."""
    adapter = _RecordingSink()
    command = _python_builder()("-c", "print('framed')")

    result = command.run_sync(output=RunOutputOptions(sink=adapter))

    assert result.ok is True
    assert adapter.opened == 1
    assert adapter.started_with is not None
    outcome = _recorded_outcome(adapter)
    assert outcome.outcome == TerminalOutcome.EXIT_ZERO
    assert outcome.exit_code == 0


def test_sink_session_closes_on_nonzero_exit() -> None:
    """A failing command still closes its session with exit_nonzero."""
    adapter = _RecordingSink()
    command = _python_builder()("-c", "raise SystemExit(3)")

    result = command.run_sync(output=RunOutputOptions(sink=adapter))

    assert result.ok is False
    assert result.exit_code == 3
    outcome = _recorded_outcome(adapter)
    assert outcome.outcome == TerminalOutcome.EXIT_NONZERO
    assert outcome.exit_code == 3


def test_sink_session_closes_on_timeout() -> None:
    """A timed-out command still closes its session with the timeout outcome."""
    adapter = _RecordingSink()
    command = _python_builder()("-c", "import time; time.sleep(2)")

    with pytest.raises(TimeoutExpired, match=r"timed out"):
        command.run_sync(
            output=RunOutputOptions(sink=adapter),
            timeout=0.1,
        )

    outcome = _recorded_outcome(adapter)
    assert outcome.outcome == TerminalOutcome.TIMEOUT
    assert outcome.exit_code is None


def test_sink_session_label_prefers_title() -> None:
    """An adapter's title attribute overrides the catalogue-derived label."""
    adapter = _RecordingSink()
    adapter.title = "Custom title"
    command = _python_builder()("-c", "print('titled')")

    result = command.run_sync(output=RunOutputOptions(sink=adapter))

    assert result.ok is True
    assert adapter.started_with is not None
    assert adapter.started_with.label == "Custom title"


def test_sink_session_label_omits_argv() -> None:
    """The derived label never contains arguments, only the program name."""
    adapter = _RecordingSink()
    secret = "s3cret-token-9f2aXq7"  # ruff: ignore[hardcoded-password-string] - synthetic test token, never a real credential.
    command = _python_builder()("-c", f"print('{secret}')")

    command.run_sync(output=RunOutputOptions(sink=adapter))

    assert adapter.started_with is not None
    assert secret not in adapter.started_with.label


# ---------------------------------------------------------------------------
# The take-once session bracket
# ---------------------------------------------------------------------------


def test_bracket_opens_once_and_closes_once() -> None:
    """The bracket returns the adapter's session and releases it on close."""
    session = _RecordingSession()
    adapter = _RecordingSink(session_factory=lambda: session)
    start = SessionStart(label="project: program", argv=("python", "-c"))

    bracket = _SinkBracket.open(adapter, start)

    assert bracket.session is session
    assert adapter.started_with == start
    assert adapter.opened == 1
    outcome = SessionOutcome(TerminalOutcome.EXIT_ZERO, exit_code=0)
    bracket.close(outcome=outcome)
    assert session.closed_with == outcome
    assert bracket.session is None, "closing must release the session"


def test_bracket_ignores_a_later_close() -> None:
    """Only the first close reaches the adapter, so its outcome stands."""
    session = _RecordingSession()
    bracket = _SinkBracket.open(
        _RecordingSink(session_factory=lambda: session),
        SessionStart(label="project: program", argv=()),
    )

    first = SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=3)
    bracket.close(outcome=first)
    bracket.close(outcome=SessionOutcome(TerminalOutcome.TIMEOUT, detail="timeout"))

    assert session.closed_with == first


def test_bracket_without_a_sink_is_empty_and_closes_silently() -> None:
    """A bracket opened over no sink owns nothing and closes nothing."""
    bracket = _SinkBracket.open(None, SessionStart(label="project: program", argv=()))

    assert bracket.session is None
    bracket.close(outcome=SessionOutcome(TerminalOutcome.ERROR))


def test_bracket_over_a_declining_adapter_is_empty() -> None:
    """An adapter that declines activation leaves an empty bracket."""
    adapter = _RecordingSink(decline=True)

    bracket = _SinkBracket.open(adapter, SessionStart(label="l", argv=()))

    assert bracket.session is None
    assert adapter.opened == 1


def test_open_sink_session_returns_the_adapters_session() -> None:
    """Opening a sink hands back the session the adapter returned."""
    session = _RecordingSession()
    adapter = _RecordingSink(session_factory=lambda: session)

    opened = _open_sink_session(adapter, SessionStart(label="l", argv=("p",)))

    assert opened is session


def test_open_sink_session_without_a_sink_is_none() -> None:
    """A run with no sink opens nothing to close later."""
    assert _open_sink_session(None, SessionStart(label="l", argv=())) is None


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
    assert _outcome_for_error(error) == expected
