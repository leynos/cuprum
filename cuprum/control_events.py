"""Aggregate events for the control operations an owned handle performs.

A handle's control operations are the ones a caller reaches for when the child
is misbehaving: a named signal, a graceful stop, a forced kill. They are also
the ones an operator most needs to see, because a refusal or an escalation is
the difference between a child that shut down when asked and one that had to be
compelled — and that difference is invisible once the run has settled.

The types here are a closed set on purpose. A control event is consumed by
adapters that turn it into metrics, and a metric label taken from an open field
is an unbounded series; a closed enum is what keeps the label set finite. The
handle's own correlation token and the child's PID travel alongside so a
control event can be joined to the run and the process it acted on, but neither
becomes a metric label, and no command line or free-form error text travels at
all: those are unbounded and would put caller data into a label.

This module is the event vocabulary only. :mod:`cuprum.control_observation`
owns registration and emission, and :mod:`cuprum.adapters.control_metrics`
projects the events onto counters.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import enum
import typing as typ

if typ.TYPE_CHECKING:
    from cuprum.events import ExecId


class ControlOperation(enum.StrEnum):
    """Closed kinds of control operation an owned handle performs."""

    SIGNAL = "signal"
    TERMINATE = "terminate"
    KILL = "kill"


class ControlOutcome(enum.StrEnum):
    """Closed terminal outcomes for one control operation.

    ``DELIVERED`` and ``ESCALATED`` describe a child that was asked to stop and
    did — the first without needing the forced signal, the second only after it.
    ``REFUSED_EXITED`` and ``REFUSED_UNSUPPORTED`` describe the two things that
    can keep a control operation from being delivered at all, and they are kept
    apart because the caller's remedy differs: an exited child is a race the
    caller simply lost, while an unsupported platform is a limitation no retry
    can fix.
    """

    DELIVERED = "delivered"
    ESCALATED = "escalated"
    REFUSED_EXITED = "refused_exited"
    REFUSED_UNSUPPORTED = "refused_unsupported"


@dc.dataclass(frozen=True, slots=True)
class ControlEvent:
    """Aggregate telemetry for one completed control operation.

    Attributes
    ----------
    operation:
        Closed operation kind, distinguishing a caller-named signal from a
        graceful stop and from a forced one.
    outcome:
        Closed completion outcome. ``escalated`` identifies a stop that had to
        send the forced signal after the grace window elapsed.
    duration_s:
        Monotonic elapsed seconds from operation start through completion.
    exec_id:
        The owning run's correlation token, when the handle safely provides
        one. ``None`` for a control operation attempted on a handle whose run
        carried no token.
    signal_number:
        The POSIX signal the caller named, for ``signal`` operations. ``None``
        for the stop operations, which choose their own signal and report
        which one ended the child through the settled outcome instead.
    pid:
        The child's process identifier, when the handle had one. It travels on
        the event so an operator reading a trace can name the process a control
        operation was aimed at; it must never become a metric label, because a
        label domain keyed on a PID grows without bound and a recycled PID
        would fold two unrelated children into one series.
    """

    operation: ControlOperation
    outcome: ControlOutcome
    duration_s: float
    exec_id: ExecId | None = None
    signal_number: int | None = None
    pid: int | None = None


type ControlHook = cabc.Callable[[ControlEvent], None]


__all__ = [
    "ControlEvent",
    "ControlHook",
    "ControlOperation",
    "ControlOutcome",
]
