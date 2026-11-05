"""Doubles and case state for the line-stream settlement state machine.

The settlement finalizer reads only a handful of execution fields and the
declared sink protocol, so these doubles record exactly what it touches: the
session close and its outcome, the delivery-worker close and its order against
the session, and the terminal events published to observers. They are test
scaffolding only and never ship.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum.events import TerminalOutcome
from cuprum.unittests._sink_test_support import RecordingSession

if typ.TYPE_CHECKING:
    from cuprum._sink_lifecycle import _SinkBracket
    from cuprum.sh import CommandResult
    from cuprum.sinks import SessionOutcome


@dc.dataclass(slots=True)
class _StubObservation:
    """Record the terminal outcomes settlement publishes to observers."""

    started_pid: int | None = 4242
    terminals: list[TerminalOutcome] = dc.field(default_factory=list)

    def emit_terminal(self, outcome: TerminalOutcome, _details: object) -> None:
        """Record one published terminal outcome."""
        self.terminals.append(outcome)


@dc.dataclass(slots=True)
class _RecordingWorker:
    """Record the delivery-worker close beside the session close."""

    order: list[str]
    closed: int = 0

    def close(self, grace: float) -> None:
        """Record a close request made on the cleanup thread."""
        del grace
        self.closed += 1
        self.order.append("delivery")

    def take_close_result(self) -> None:
        """Report no deferred delivery diagnostics."""


@dc.dataclass(frozen=True, slots=True)
class _StubExecution:
    """Supply the fields settlement reads from a started run."""

    observation: _StubObservation
    delivery_worker: _RecordingWorker | None
    delivery_grace_s: float | None = None


class _OrderedSession(RecordingSession):
    """Record the session close in the machine's shared ordering log."""

    def __init__(self, order: list[str]) -> None:
        """Start an open session recording its close into *order*."""
        super().__init__()
        self._order = order

    def close(self, outcome: SessionOutcome) -> None:
        """Record the close, then defer to the recording double."""
        self._order.append("session")
        super().close(outcome)


@dc.dataclass(frozen=True, slots=True)
class _Teardown:
    """The teardown failures one generated case stages.

    ``hook`` fails while settlement drains an observe-hook task; ``published``
    fails the result future the coordinator was expected to publish.
    """

    hook: BaseException | None = None
    published: BaseException | None = None


_NO_TEARDOWN = _Teardown()


@dc.dataclass(frozen=True, slots=True)
class _Settlement:
    """One generated ending handed to the settlement finalizer."""

    bracket: _SinkBracket
    execution: _StubExecution | None
    result: CommandResult | None
    failure: BaseException | None
    errors: _Teardown = _NO_TEARDOWN


@dc.dataclass(slots=True)
class _Case:
    """The generated shape of one settlement case, plus what it produced."""

    order: list[str] = dc.field(default_factory=list)
    started: bool = False
    bracket: _SinkBracket | None = None
    session: _OrderedSession | None = None
    worker: _RecordingWorker | None = None
    execution: _StubExecution | None = None
    settled: bool = False
    result: CommandResult | None = None
    failure: BaseException | None = None
    expected: TerminalOutcome = TerminalOutcome.CANCELLED
    raised: BaseException | None = None
