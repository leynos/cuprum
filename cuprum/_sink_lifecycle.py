"""Shared presentation-sink session lifecycle for command and pipeline runs.

Every run brackets its presentation sink the same way: open the adapter's
session once, before the work starts, and close it once on each terminal path
with a bounded ``SessionOutcome``. :mod:`cuprum.sh` and the pipeline driver in
:mod:`cuprum._pipeline_internals` both owe that bracket, so the mechanics live
here rather than inside either execution layer, and neither owns them alone.
The adapter-facing protocol itself stays in :mod:`cuprum.sinks`; nothing here
knows about any concrete adapter.

``TimeoutExpired`` is resolved when :func:`_outcome_for_error` runs rather than
at import time: :mod:`cuprum.sh` owns that exception and imports this module.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum.sinks import base as sinks

if typ.TYPE_CHECKING:
    from cuprum.sh import CommandResult, SafeCmd

_DEFAULT_LABEL_SEPARATOR = ": "


def _run_label(cmd: SafeCmd) -> str:
    """Build the bounded display label for one run.

    Derived from catalogue metadata as ``"<project>: <program>"``. Never
    includes argv: catalogue membership does not make arguments safe to print,
    so secrets cannot leak into the label here. An adapter that wants a
    different label applies its own configuration inside ``open_session``;
    this layer deliberately reads nothing but the declared protocol, so an
    adapter's private attributes cannot reach the shared lifecycle.

    Returns
    -------
    str
        The bounded display label for the run.
    """
    return f"{cmd.project.name}{_DEFAULT_LABEL_SEPARATOR}{cmd.program}"


def _command_session_start(cmd: SafeCmd) -> sinks.SessionStart:
    """Build the session start that frames one single-command run."""
    return sinks.SessionStart(
        label=_run_label(cmd),
        argv=cmd.argv_with_program,
    )


@dc.dataclass(slots=True)
class _SinkBracket:
    """The take-once owner of one run's presentation-sink session.

    A bracket is created where the session opens and every terminal path
    finalizes through it, so the close happens exactly once per run — even
    when an early path records a precise outcome and a later guard would
    otherwise close again. The first close releases the session; every later
    close is a no-op.

    Attributes
    ----------
    session:
        The active session, or ``None`` when no sink was configured or the
        adapter declined activation. Stream routing reads it; closing clears
        it.
    """

    session: sinks.OutputSession | None

    @classmethod
    def open(
        cls,
        sink: sinks.OutputSink | None,
        start: sinks.SessionStart,
    ) -> _SinkBracket:
        """Open the run's session, if any, under a fresh bracket.

        Called once per invocation before the subprocess starts. An adapter
        that declines activation leaves the bracket empty and the run keeps
        the plain destinations.

        Returns
        -------
        _SinkBracket
            The bracket owning this run's session, which may be empty.
        """
        return cls(_open_sink_session(sink, start))

    def close(self, *, outcome: sinks.SessionOutcome) -> None:
        """Finalize the owned session, recording *outcome* on the way out.

        Parameters
        ----------
        outcome : sinks.SessionOutcome
            The terminal report for the run.
        """
        session, self.session = self.session, None
        _close_sink_session(session, outcome=outcome)


def _open_sink_session(
    sink: sinks.OutputSink | None,
    start: sinks.SessionStart,
) -> sinks.OutputSession | None:
    """Open the presentation sink's session for this run, if any.

    Framing is the adapter's own business and is complete by the time it
    returns: the session's log destination is written before the subprocess
    starts, so child output can never appear above the adapter's opening
    frame. An adapter that declines activation returns ``None`` and the run
    keeps the plain destinations; there is no state to unwind in that case.

    Returns
    -------
    sinks.OutputSession | None
        The active session, or ``None`` when no sink is configured or the
        adapter declines activation.
    """
    if sink is None:
        return None
    return sink.open_session(start)


def _close_sink_session(
    session: sinks.OutputSession | None,
    *,
    outcome: sinks.SessionOutcome,
) -> None:
    """Finalize a presentation-sink session on any terminal path.

    Called during shielded cleanup so cancellation cannot abandon the close
    part-way. The adapter's ``close`` is idempotent by protocol, so a caller
    that inspects the result after the run cannot double-finalize the session.

    Parameters
    ----------
    session : sinks.OutputSession | None
        The active session, or ``None`` when no sink was configured.
    outcome : sinks.SessionOutcome
        The terminal report for the run.
    """
    if session is None:
        return
    session.close(outcome)


def _outcome_for_result(result: CommandResult) -> sinks.SessionOutcome:
    """Map a completed command's result onto the terminal-outcome set."""
    outcome = (
        sinks.TerminalOutcome.EXIT_ZERO
        if result.exit_code == 0
        else sinks.TerminalOutcome.EXIT_NONZERO
    )
    return sinks.SessionOutcome(outcome=outcome, exit_code=result.exit_code)


def _outcome_for_error(error: BaseException) -> sinks.SessionOutcome:
    """Map a run's terminal error onto the terminal-outcome set.

    The execution layer never passes exception text as the detail: only the
    bounded categorical categories below reach the adapter, so exception
    messages and argv cannot leak into workflow-log annotations.

    Returns
    -------
    sinks.SessionOutcome
        The terminal report for the run.
    """
    from cuprum.sh import TimeoutExpired

    match error:
        case TimeoutExpired():
            return sinks.SessionOutcome(
                outcome=sinks.TerminalOutcome.TIMEOUT,
                exit_code=None,
                detail="timeout",
            )
        case asyncio.CancelledError():
            return sinks.SessionOutcome(outcome=sinks.TerminalOutcome.CANCELLED)
        case _:
            return sinks.SessionOutcome(outcome=sinks.TerminalOutcome.ERROR)


__all__ = [
    "_SinkBracket",
    "_close_sink_session",
    "_command_session_start",
    "_open_sink_session",
    "_outcome_for_error",
    "_outcome_for_result",
    "_run_label",
]
