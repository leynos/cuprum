"""Registration and fail-open emission for owned-handle control events.

A control operation is the point at which a caller intervenes in a child's
life, and its outcome is the fact an operator needs afterwards: was the child
asked to stop and did it comply, or did the handle have to compel it, or was
the request refused because the child was already gone? None of that survives
in the settled result, which reports how the child ended rather than what was
done to end it.

Emission is deliberately fail-open. A control operation has a job to do — the
child has to be signalled or stopped — and an observer that raises must not be
able to prevent that, nor to replace the refusal the caller is owed with an
observer's own error. Every hook is therefore invoked inside a guard that logs
the failure and moves on, and the same guard closes a coroutine an observer
returned by mistake, so an incorrectly declared async hook cannot leak a never-awaited
warning.

Registration lives in a :class:`~contextvars.ContextVar`, matching the other
observation channels: a hook is visible to the run that registered it and to
nothing else, so two concurrent runs in one process cannot observe each other's
control operations.
"""

from __future__ import annotations

import dataclasses as dc
import inspect
import logging
import time
import typing as typ
from contextvars import ContextVar

from cuprum._scope_registration import _IdentityTupleRegistration
from cuprum.control_events import (
    ControlEvent,
    ControlHook,
    ControlOperation,
    ControlOutcome,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecId

_LOGGER = logging.getLogger(__name__)
_control_hooks: ContextVar[tuple[ControlHook, ...]] = ContextVar(
    "cuprum_control_hooks",
    default=(),
)


@dc.dataclass(frozen=True, slots=True)
class _ControlTarget:
    """Which run and child one control event describes.

    Grouped rather than passed as three loose optionals because they travel
    together and are meaningless apart: a
    :class:`~cuprum.control_events.ControlEvent` is only useful to an operator
    when it can say which run, and usually which process, the operation was
    aimed at.
    """

    exec_id: ExecId | None = None
    signal_number: int | None = None
    pid: int | None = None


@dc.dataclass(slots=True)
class _ControlMeasurement:
    """Bound the duration of one control operation until it completes."""

    operation: ControlOperation
    hooks: tuple[ControlHook, ...]
    monotonic_clock: cabc.Callable[[], float]
    started_s: float
    target: _ControlTarget

    def complete(self, outcome: ControlOutcome) -> None:
        """Emit the single completion event for this control operation."""
        _emit_control_event(
            ControlEvent(
                operation=self.operation,
                outcome=outcome,
                duration_s=self.monotonic_clock() - self.started_s,
                exec_id=self.target.exec_id,
                signal_number=self.target.signal_number,
                pid=self.target.pid,
            ),
            hooks=self.hooks,
        )


def current_control_hooks() -> tuple[ControlHook, ...]:
    """Return control hooks registered in the current context."""
    return _control_hooks.get()


class ControlHookRegistration(_IdentityTupleRegistration["ControlHook"]):
    """Registration handle for a control observation hook.

    Detach removes only this handle's own hook by identity rather than
    restoring a captured token, so detaching out of last-in-first-out order
    cannot resurrect a hook someone already removed.
    """

    __slots__ = ()

    def __init__(self, hook: ControlHook) -> None:
        """Append ``hook`` to the current context's control hooks."""
        super().__init__(_control_hooks, hook)


def observe_control(hook: ControlHook) -> ControlHookRegistration:
    """Register a hook for owned-handle control events.

    Parameters
    ----------
    hook:
        Synchronous callable invoked once for each completed control operation.
        It must not raise; a failure is logged and swallowed so it cannot
        replace the control operation's own outcome.

    Returns
    -------
    ControlHookRegistration
        A context-local registration handle that supports ``detach()`` and
        context-manager use.

    """
    return ControlHookRegistration(hook)


def _start_control(
    operation: ControlOperation,
    *,
    target: _ControlTarget | None = None,
    monotonic_clock: cabc.Callable[[], float] = time.monotonic,
) -> _ControlMeasurement | None:
    """Start measuring a control operation only when a hook has opted in.

    Parameters
    ----------
    operation:
        The closed operation being measured.
    target:
        The run and child the operation is aimed at. Defaults to an empty
        target, for a producer that has neither.
    monotonic_clock:
        The clock the duration is measured against.

    Returns
    -------
    _ControlMeasurement | None
        The measurement to complete, or ``None`` when nothing is observing, so
        an unobserved run pays no clock read and allocates nothing.

    """
    hooks = current_control_hooks()
    if not hooks:
        return None
    return _ControlMeasurement(
        operation=operation,
        hooks=hooks,
        monotonic_clock=monotonic_clock,
        started_s=monotonic_clock(),
        target=target if target is not None else _ControlTarget(),
    )


def _complete_control(
    measurement: _ControlMeasurement | None,
    outcome: ControlOutcome,
) -> None:
    """Emit completion when the control operation started under an observer."""
    if measurement is not None:
        measurement.complete(outcome)


def _emit_control_event(
    event: ControlEvent,
    *,
    hooks: tuple[ControlHook, ...],
) -> None:
    """Deliver an event without allowing observer failures to affect control.

    ``BaseException`` is caught rather than ``Exception``, and the difference is
    load-bearing rather than defensive. ``asyncio.CancelledError`` derives from
    ``BaseException``, so an observer raising one would escape this guard into
    the worst possible place. The stop path emits this event before it starts
    settling, so an escaping cancellation abandons the settle and leaves a
    reaped child with no result recorded; the signal path emits it where an
    escaping cancellation replaces the public refusal the caller is owed.

    An observer raising ``KeyboardInterrupt`` or ``SystemExit`` is reported and
    swallowed like any other observer failure. Letting either through would
    abandon the same in-flight control operation, which is the harm this guard
    exists to prevent, and the operation a hook runs inside is a bounded
    synchronous call rather than a place an interpreter-level exit belongs.
    """
    for hook in hooks:
        try:
            result = hook(event)
        except BaseException as exc:
            _LOGGER.warning(
                "control_observer_failed operation=%s outcome=%s error=%s",
                event.operation,
                event.outcome,
                type(exc).__name__,
                exc_info=True,
                extra={
                    "cuprum_action": "control_observer_failed",
                    "cuprum_operation": str(event.operation),
                    "cuprum_outcome": str(event.outcome),
                    "cuprum_error_type": type(exc).__name__,
                },
            )
            continue
        if result is not None:
            _discard_hook_result(result, event)


def _discard_hook_result(result: object, event: ControlEvent) -> None:
    """Report non-``None`` hook returns and close accidental coroutines."""
    _LOGGER.warning(
        "control_observer_returned_value operation=%s result_type=%s",
        event.operation,
        type(result).__name__,
        extra={
            "cuprum_action": "control_observer_returned_value",
            "cuprum_operation": str(event.operation),
            "cuprum_result_type": type(result).__name__,
        },
    )
    if inspect.iscoroutine(result):
        result.close()


__all__ = [
    "ControlHookRegistration",
    "current_control_hooks",
    "observe_control",
]
