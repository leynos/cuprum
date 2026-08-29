"""Registration and emission for Rust-pump routing events.

Pump hooks live on their own :class:`~contextvars.ContextVar` rather than on
:class:`~cuprum.context.CuprumContext`. That is the point of the design, not an
implementation shortcut: ``ScopeConfig``, ``CuprumContext.narrow``, and every
consumer that reads the execution context keep exactly the shape they had, so
adding this channel cannot alter how an existing caller's commands execute.
Registration still follows the repository's token-restoration discipline, so a
pump registration nests and detaches like every other one.

See :mod:`cuprum.pump_events` for the event types this channel carries.
"""

from __future__ import annotations

import inspect
import logging
import typing as typ
from contextvars import ContextVar

if typ.TYPE_CHECKING:
    from contextvars import Token

    from cuprum.pump_events import PumpEvent, PumpHook

_LOGGER = logging.getLogger(__name__)

_pump_hooks: ContextVar[tuple[PumpHook, ...]] = ContextVar(
    "cuprum_pump_hooks",
    default=(),
)


def current_pump_hooks() -> tuple[PumpHook, ...]:
    """Return the pump hooks registered in the current context.

    Returns
    -------
    tuple[PumpHook, ...]
        The registered hooks in registration order, empty when none are
        registered.

    Examples
    --------
    Registration is scoped, so the tuple is empty again on exit::

        with observe_pump(record):
            assert current_pump_hooks() == (record,)
        assert current_pump_hooks() == ()

    """
    return _pump_hooks.get()


class PumpHookRegistration:
    """Registration handle for a Rust-pump observation hook.

    Supports ``detach()`` and context-manager use. The handle captures a
    :class:`~contextvars.Token` when it installs the extended hook tuple, and
    :meth:`detach` restores the exact tuple that was current when the handle was
    created — the same discipline
    :class:`cuprum.context.registration._TokenRegistration` applies to execution
    contexts. Detach in the :class:`~contextvars.Context` that created the
    registration; resetting a ``ContextVar`` with a token from another context
    raises :class:`ValueError`.

    Prefer ``with`` blocks, which detach in last-in-first-out order; detaching
    out of order restores a tuple that discards later registrations.
    """

    __slots__ = ("_detached", "_hook", "_token")

    def __init__(self, hook: PumpHook) -> None:
        """Append ``hook`` to the current context's pump hooks."""
        self._hook = hook
        self._detached = False
        self._token: Token[tuple[PumpHook, ...]] = _pump_hooks.set((
            *_pump_hooks.get(),
            hook,
        ))

    def detach(self) -> None:
        """Restore the pump hooks that preceded this registration."""
        if self._detached:
            return
        _pump_hooks.reset(self._token)
        self._detached = True

    def __enter__(self) -> typ.Self:
        """Enter the context manager; the hook is already registered."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Detach the registration on scope exit."""
        self.detach()


def observe_pump(hook: PumpHook) -> PumpHookRegistration:
    """Register a hook for Rust-pump routing events in the current context.

    Parameters
    ----------
    hook:
        Synchronous callable invoked with each
        :class:`~cuprum.pump_events.PumpEvent`.

    Returns
    -------
    PumpHookRegistration
        A handle that can be detached or used as a context manager.

    Examples
    --------
    Count declined hand-offs into a metrics collector::

        from cuprum.adapters.metrics_adapter import InMemoryMetrics
        from cuprum.adapters.pump_metrics import PumpMetricsHook

        metrics = InMemoryMetrics()
        with observe_pump(PumpMetricsHook(metrics)):
            pipeline.run_sync()

    """
    return PumpHookRegistration(hook)


def _emit_pump_event(event: PumpEvent) -> None:
    """Deliver ``event`` to every registered pump hook.

    Hook-failure policy
    -------------------
    A hook that raises :class:`Exception` is reported at ``WARNING`` with its
    traceback and the remaining hooks still run. This deliberately diverges from
    :func:`cuprum._observability._emit_exec_event`, which re-raises so a broken
    observe hook fails the command it was observing. Both emission sites here
    sit on paths whose contract is that they must complete: one is the
    fall-back that keeps a hop working when the fast path is unavailable, the
    other is cancellation unwinding, where a raise would replace the
    ``CancelledError`` the caller asked for. Propagating would let registering a
    metrics backend turn a pipeline that would have succeeded into one that
    fails — a change in execution behaviour caused by observing it, which is
    exactly what this channel exists to avoid. The failure is recorded, not
    swallowed; ``pump_observer_failed`` names the hook's error type and the
    event that provoked it.

    Anything that is not an :class:`Exception` — ``SystemExit``,
    ``KeyboardInterrupt``, ``asyncio.CancelledError`` — propagates untouched, on
    the same reasoning as :func:`cuprum._pipeline_streams._await_rust_pump`: a
    shutdown signal arriving here must still travel.

    With no hooks registered this returns before touching the event, so a caller
    that has not opted in pays nothing and behaves exactly as before.
    """
    hooks = _pump_hooks.get()
    if not hooks:
        return
    for hook in hooks:
        _invoke_pump_hook(hook, event)


def _invoke_pump_hook(hook: PumpHook, event: PumpEvent) -> None:
    """Invoke one pump hook, reporting rather than propagating its failure."""
    try:
        result = hook(event)
    except Exception as exc:
        _LOGGER.warning(
            "pump_observer_failed phase=%s reason=%s error=%s",
            event.phase,
            event.reason,
            type(exc).__name__,
            exc_info=True,
            extra={
                "cuprum_action": "pump_observer_failed",
                "cuprum_phase": event.phase,
                "cuprum_reason": event.reason,
                "cuprum_error_type": type(exc).__name__,
            },
        )
        return
    if result is not None:
        _discard_hook_result(result, event)


def _discard_hook_result(result: object, event: PumpEvent) -> None:
    """Report a non-``None`` hook return, closing coroutines it left behind.

    Pump hooks are synchronous by contract. An unawaited coroutine would
    otherwise surface later as an unrelated ``coroutine was never awaited``
    warning, so it is closed here and reported against the event that produced
    it.
    """
    _LOGGER.warning(
        "pump_observer_returned_value phase=%s result_type=%s",
        event.phase,
        type(result).__name__,
        extra={
            "cuprum_action": "pump_observer_returned_value",
            "cuprum_phase": event.phase,
            "cuprum_result_type": type(result).__name__,
        },
    )
    if inspect.iscoroutine(result):
        result.close()


__all__ = [
    "PumpHookRegistration",
    "current_pump_hooks",
    "observe_pump",
]
