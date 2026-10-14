"""What a control operation is reported as, in the channel's closed vocabulary.

The handle's public verbs decide *what to do* to a child; this module decides
*what to say about it*. A control operation reaches the observation channel as
one :class:`~cuprum.control_events.ControlOutcome`, and three different
situations produce one:

- a signal the platform cannot deliver, or one aimed at a child that has
  already exited, is that refusal — and it is named by the error the delivery
  helper raised, because the helper is the only party that knows which of the
  two it found;
- a stop that delivered nothing against a child still live is a refusal too,
  even though the stop helpers honestly report it as an ordinary non-delivery;
  and
- anything else is a delivery or an escalation, read off the outcome directly.

The classification is stated once, here, rather than inline at each verb, so
the three answers cannot drift apart as the verbs change.

The unobserved case is deliberately not a fourth: when no observer is
registered the measurement is ``None`` and nothing is reported at all, which is
why every function here tolerates it.
"""

from __future__ import annotations

import typing as typ

from cuprum._process_handle_errors import (
    SignalAfterExitError,
    UnsupportedSignalError,
)
from cuprum.control_events import ControlOutcome

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._process_lifecycle import _TerminationOutcome

__all__ = [
    "_refusal_outcome",
    "_weighted_stop",
]

_REFUSAL_BY_ERROR: dict[type[BaseException], ControlOutcome] = {
    UnsupportedSignalError: ControlOutcome.REFUSED_UNSUPPORTED,
    SignalAfterExitError: ControlOutcome.REFUSED_EXITED,
}


def _refusal_outcome(exc: BaseException) -> ControlOutcome | None:
    """Name the refusal ``exc`` reports, or ``None`` when it reports none.

    A delivery helper raises for two reasons that are outcomes of the control
    operation rather than failures of it, and each maps to the outcome that
    says so. An error this table does not recognize reports nothing: it
    propagates unobserved rather than being folded into a reason a caller would
    misread as a refusal the handle made deliberately.

    Returns
    -------
    ControlOutcome | None
        The refusal to report, or ``None`` when the error is not one the
        control channel has a vocabulary for.
    """
    return _REFUSAL_BY_ERROR.get(type(exc))


def _weighted_stop(
    outcome: _TerminationOutcome,
    process: asyncio.subprocess.Process,
) -> _TerminationOutcome:
    """Rewrite a stop that delivered nothing into the refusal it is.

    The stop helpers report a signal they could not send as an ordinary
    "nothing was delivered" outcome, which is honest for their caller but
    ambiguous for an observer: a forced kill into a child that had already
    exited reads exactly like a graceful stop that found the same thing. What
    separates them is the child's own status — an unexited child that took no
    signal at all means the delivery was refused, because on any platform the
    graceful path reaches, ``terminate`` and ``kill`` both succeed against a
    live child. The status is read back here rather than inside the stop
    helper, which deliberately stays free of any dependency on the control
    vocabulary.

    Returns
    -------
    _TerminationOutcome
        The outcome unchanged when a signal reached the child, or when the
        child had already exited on its own; otherwise the same outcome with
        :attr:`~cuprum._process_lifecycle._TerminationOutcome.refused` set.
    """
    if outcome.delivered or process.returncode is not None:
        return outcome
    return outcome._replace(refused=True)
