"""Judgements a stateful settlement model makes about one run's ending.

``_running_command_support`` supplies the doubles and the harness the
settlement machine drives; this module supplies the *decisions* it draws from
them. Separating the two keeps each file inside the 400-line cap and, more
usefully, keeps the rules in ``test_running_command_stateful`` about the
orderings they explore rather than about how a recorded exit code is spelled.

Everything here is a pure function of what a stop did and what the child
reported. The machine asserts those claims against the handle's real record, so
a disagreement between this model and production surfaces as a failing case.
"""

from __future__ import annotations

import typing as typ

from cuprum.events import ExecEvent, TerminalOutcome
from cuprum.unittests._running_command_support import _KILLED, _TERMINATED

if typ.TYPE_CHECKING:
    from cuprum.sh import CommandResult

# The exit codes a child reports after a stop, as the doubles define them.
_STOP_CODES = frozenset({_TERMINATED, _KILLED})


def _expected_outcome(report: str, exit_code: int) -> TerminalOutcome:
    """Return the terminal outcome a run that ended this way must publish."""
    if report == "timeout":
        return TerminalOutcome.TIMEOUT
    succeeded = report == "ordinary" and exit_code == 0
    return TerminalOutcome.EXIT_ZERO if succeeded else TerminalOutcome.EXIT_NONZERO


def _stop_code(*, terminated: int, grace: int) -> int:
    """Return the status a stop of this step left on the child.

    A stop whose grace window was enough leaves the graceful signal's status;
    one that had to be compelled leaves the forced signal's. *terminated*
    counts the SIGTERMs the child has received and *grace* how many of those an
    earlier step already accounted for, so this asks whether *this* step
    stopped the child by grace rather than whether it was ever signalled at
    all. The two differ: the SIGTERM-immune child in
    ``cancel_a_stop_inside_its_grace`` takes one of each and must report the
    forced status, which is why the pre-counted SIGTERM is subtracted first.

    Returns
    -------
    int
        The exit code the child must report for that stop.
    """
    return _TERMINATED if terminated > grace else _KILLED


def _reports_a_stop(result: CommandResult) -> bool:
    """Whether *result* carries a status only a signal could have produced."""
    return result.exit_code in _STOP_CODES


def _settled_event(events: list[ExecEvent]) -> ExecEvent | None:
    """Return the single terminal event *events* holds, if it holds one."""
    return next((item for item in events if item.phase == "settled"), None)
