"""Typed output-adapter protocol for presentation sinks.

An adapter opts a run into an alternative *presentation* of the parent-facing
output: framing, annotations, or routing. It never changes capture, success
semantics, or the returned result. The protocol is deliberately narrow — a
run opens a session, writes through the writers the session hands back, and
closes the session with a terminal outcome. All protocol knowledge (for
example, GitHub Actions workflow commands) stays inside the adapter
implementation, which is why :mod:`cuprum.sinks` has no Actions imports here.

Lifecycle contract for implementations:

1. :meth:`OutputSink.open_session` is called once per invocation, before the
   subprocess starts. It returns a fresh :class:`OutputSession` (or ``None``
   to decline activation for this run). Mutable framing state belongs to the
   session, never to the adapter instance, so one adapter can serve many
   sequential runs and concurrent runs remain independent.
2. The run writes child output and its own diagnostics through
   :attr:`OutputSession.log` only. Capture is unaffected.
3. :meth:`OutputSession.close` is called exactly once, on every terminal
   path — success, non-zero exit, timeout, cancellation, or an earlier
   failure — through the execution layer's shielded finalization. It must be
   idempotent in case a caller-inspected run is finalized twice.

An adapter is inactive for a run when it returns ``None`` from
``open_session``; the run then uses the plain destinations unchanged.
"""

from __future__ import annotations

import dataclasses as dc
import enum
import typing as typ


class TerminalOutcome(enum.StrEnum):
    """Why a run finished, as a closed set the adapter can act on.

    Examples
    --------
    The member value is the string adapters see::

        assert TerminalOutcome.EXIT_NONZERO == "exit_nonzero"

    """

    EXIT_ZERO = "exit_zero"
    EXIT_NONZERO = "exit_nonzero"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    ERROR = "error"


@dc.dataclass(frozen=True, slots=True)
class SessionStart:
    """Immutable inputs an adapter needs to frame one run.

    Attributes
    ----------
    label:
        Bounded, already-safe display label for the group (for example
        ``"project: program"``). The execution layer builds it from catalogue
        metadata, not from argv, so secrets never reach it.
    argv:
        The full command vector (program followed by arguments) the run will
        execute. Adapters that frame a run per command — for example a GitHub
        Actions group titled with the program args — use this directly. It is
        caller-controlled input: an adapter that surfaces it verbatim opts the
        caller into displaying it (GitHub Actions groups are visible in the
        workflow log regardless, because echoed child output is shown there).

    """

    label: str
    argv: tuple[str, ...]


@dc.dataclass(frozen=True, slots=True)
class SessionOutcome:
    """Immutable terminal report for one run.

    Attributes
    ----------
    outcome:
        The closed-set terminal category.
    exit_code:
        The actual exit code when the run produced one, otherwise ``None``.
        Adapters must not synthesize shell-style codes.
    detail:
        Optional bounded, categorical detail (for example ``"timeout"``).
        The execution layer never passes exception text or argv here.

    """

    outcome: TerminalOutcome
    exit_code: int | None = None
    detail: str | None = None


class OutputSession(typ.Protocol):
    """The write side of one active adapter run.

    Implementations keep their framing state here and remain safe for the
    sequential single-writer use the execution layer guarantees: child-output
    writes and control writes are issued from the run's own task context, not
    from unrelated tasks.
    """

    @property
    def log(self) -> typ.IO[str]:
        """The ordered parent-facing log destination for this run.

        Mirrored child output and run diagnostics are routed through this one
        writer so their relative order in the log is the order the adapter
        received them. Implementations may back it with the parent's stderr
        or a caller-supplied sink.

        Returns
        -------
        IO[str]
            A writable text destination.
        """

    def close(self, outcome: SessionOutcome) -> None:
        """Finish the session, emitting any terminal framing and annotation.

        Implementations must be idempotent: a second call with any outcome
        performs no further writes and swallows no error twice. This is the
        only method that may emit failure annotations, and it runs during
        shielded finalization so cancellation cannot abandon it part-way.

        Parameters
        ----------
        outcome : SessionOutcome
            The terminal report for the run.
        """


class OutputSink(typ.Protocol):
    """A factory for per-run presentation sessions.

    Implementations hold only immutable configuration. All mutable state
    lives on the :class:`OutputSession` returned by
    :meth:`open_session`, which the execution layer creates fresh for every
    invocation.
    """

    def open_session(self, start: SessionStart) -> OutputSession | None:
        """Begin a presentation session for one run, or decline.

        Returning ``None`` deactivates the adapter for this run without
        affecting other runs; the execution layer then routes output exactly
        as if no sink had been supplied.

        Parameters
        ----------
        start : SessionStart
            Bounded run metadata (the display label for the group).

        Returns
        -------
        OutputSession | None
            The active session, or ``None`` when the adapter stays inactive
            for this run.
        """


__all__ = [
    "OutputSession",
    "OutputSink",
    "SessionOutcome",
    "SessionStart",
    "TerminalOutcome",
]
