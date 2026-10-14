"""Record control operations as events on the execution span they act on.

A control operation — a signal, a termination, a kill — is part of its run's
story rather than a run of its own, so it belongs on the span that story is
already told on. That is why the join is a second registration rather than a
span of its own: the span's lifetime is the execution's, and a control span
would either outlive the run or have to be parented across two channels to be
placed correctly.

Two registrations are needed and both are explicit. Control events travel on
their own channel, per
[ADR-008](../../docs/adr-008-rust-pump-observation-channel.md), so a caller who
wants them traced asks for them; a caller who does not pays nothing and sees no
behavioural change.

The projection is deliberately narrow. Everything attached here is bounded —
the operation, its closed outcome, its duration, and the two identifiers the
run already publishes — so a backend indexing these attributes sees a small,
stable set of values rather than one new series per execution.
"""

from __future__ import annotations

import typing as typ

from cuprum.control_observation import (
    ControlHookRegistration,
    observe_control,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.adapters.tracing_adapter import TracingHook
    from cuprum.control_events import ControlEvent
    from cuprum.events import ExecId

__all__ = ["_ControlJoinTracingMixin", "record_control_events"]


class _ControlJoinTracingMixin:
    """Add one bounded control event to an open execution span."""

    def record_event_on(
        self,
        exec_id: ExecId | None,
        name: str,
        attributes: cabc.Mapping[str, object] | None = None,
    ) -> None:
        """Record one bounded event on the open span for ``exec_id``.

        This is the seam a control event reaches an execution span through. It
        takes a token rather than an :class:`~cuprum.events.ExecEvent` because
        control events are a separate channel and have no phase to match on;
        the token is what every channel in this library correlates by.

        An event for a token with no open span is dropped rather than starting
        one, because a span outliving the execution it describes would report
        a duration the run never had.

        Parameters
        ----------
        exec_id:
            The owning run's correlation token, or ``None`` when the producer
            has none; ``None`` is dropped for the same reason a settled token
            is.
        name:
            The span-event name, already namespaced by the caller.
        attributes:
            Bounded attributes for the event.
        """
        if exec_id is None:
            return
        hook = typ.cast("TracingHook", self)
        active = hook._lookup_exec_span(exec_id)
        if active is None:
            return
        with active.lock:
            if not active.is_closed:
                active.span.add_event(name, dict(attributes or {}))


class _ControlSpanRecorder:
    """Project one control event onto its run's open execution span."""

    __slots__ = ("_hook",)

    def __init__(self, hook: TracingHook) -> None:
        """Record control events on spans opened by ``hook``."""
        self._hook = hook

    def __call__(self, event: ControlEvent) -> None:
        """Add one bounded control event to the owning execution span."""
        attributes: dict[str, object] = {
            "cuprum.operation": str(event.operation),
            "cuprum.outcome": str(event.outcome),
            "cuprum.duration_s": event.duration_s,
        }
        if event.pid is not None:
            attributes["cuprum.pid"] = event.pid
        if event.signal_number is not None:
            # Coerced because callers spell signals with ``signal.Signals``,
            # an ``IntEnum``, and a backend that validates attribute types
            # against the ``Span`` protocol's primitives may reject the enum
            # rather than unwrap it. The value is already an int; this only
            # stops it arriving as one's subclass.
            attributes["cuprum.signal_number"] = int(event.signal_number)
        self._hook.record_event_on(
            event.exec_id,
            f"cuprum.control.{event.operation}",
            attributes,
        )


def record_control_events(hook: TracingHook) -> ControlHookRegistration:
    """Record control operations as events on the execution span they act on.

    Parameters
    ----------
    hook:
        The tracing hook whose spans the control events are recorded on. It
        must already be registered as an observe hook, because this function
        reports events onto spans that hook has opened.

    Returns
    -------
    ControlHookRegistration
        A detachable registration. It fails open: an observer that raises is
        logged and skipped so it can never replace a control operation's own
        error or interrupt the child cleanup the operation is performing.
    """
    return observe_control(_ControlSpanRecorder(hook))
