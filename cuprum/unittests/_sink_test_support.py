"""Shared in-memory presentation-sink doubles for run-lifecycle tests.

The shared lifecycle in ``cuprum._sink_lifecycle`` reads only the declared
:class:`~cuprum.sinks.base.OutputSink` protocol, so a double needs no adapter
behaviour beyond recording what it was handed. These two classes are that
double, kept here rather than duplicated in each lifecycle module because the
contract they prove — one session per run, closed exactly once with a bounded
outcome — is the same one every caller is asserting.

``cuprum/unittests`` is excluded from the built wheel, so this module is
test-only scaffolding and never ships.
"""

from __future__ import annotations

import io
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sinks import OutputSession, SessionOutcome, SessionStart


class RecordingSession:
    """Minimal ``OutputSession`` recording its log writes and close outcome.

    Attributes
    ----------
    log_io:
        The in-memory destination this session hands to the run.
    closed_with:
        The outcome of the first ``close``, or ``None`` while still open.
    closed:
        How many times ``close`` was called, including idempotent repeats.
        A terminal path that closes twice is a defect the run owes the
        adapter, so callers assert this is exactly one.
    """

    def __init__(self) -> None:
        """Start with an in-memory log and no closed outcome."""
        self.log_io = io.StringIO()
        self.closed_with: SessionOutcome | None = None
        self.closed = 0

    @property
    def log(self) -> typ.IO[str]:
        """The in-memory log destination for this run."""
        return self.log_io

    @property
    def framed(self) -> str:
        """Everything the run wrote through this session, in arrival order."""
        return self.log_io.getvalue()

    def close(self, outcome: SessionOutcome) -> None:
        """Record the terminal outcome and count the close calls."""
        self.closed += 1
        if self.closed_with is None:
            self.closed_with = outcome


class RecordingSink:
    """Minimal ``OutputSink`` handing out a fresh :class:`RecordingSession`.

    Deliberately carries no ``title``: the shared lifecycle reads only the
    declared protocol, so an adapter's private configuration cannot reach
    ``SessionStart.label``. A subclass that adds one is testing that boundary,
    not this contract.
    """

    def __init__(
        self,
        *,
        decline: bool = False,
        session_factory: cabc.Callable[[], RecordingSession] = RecordingSession,
    ) -> None:
        """Configure whether the adapter declines, and how sessions are made."""
        self.decline = decline
        self.session_factory = session_factory
        self.started_with: SessionStart | None = None
        self.opened = 0
        self.sessions: list[RecordingSession] = []

    def open_session(self, start: SessionStart) -> OutputSession | None:
        """Record the start and return a fresh recording session."""
        self.started_with = start
        self.opened += 1
        if self.decline:
            return None
        session = self.session_factory()
        self.sessions.append(session)
        return session

    @property
    def last_session(self) -> RecordingSession:
        """The most recently opened session, asserting one was opened."""
        assert self.sessions, (
            f"the run must have opened a sink session; the adapter was consulted "
            f"{self.opened} time(s) and returned none"
        )
        return self.sessions[-1]

    @property
    def recorded_outcome(self) -> SessionOutcome:
        """The outcome the most recent session was closed with."""
        session = self.last_session
        assert session.closed_with is not None, (
            "every terminal path must close the sink session, whatever the "
            "run's outcome"
        )
        return session.closed_with
