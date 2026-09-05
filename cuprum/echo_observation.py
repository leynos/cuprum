"""Registration and emission for stream-echo failure events.

Echo hooks live on their own :class:`~contextvars.ContextVar`, mirroring
:mod:`cuprum.pump_observation`: adding the channel cannot alter how an existing
caller's commands execute, because a caller opts in by registering a hook and a
caller that does not pays nothing. Registration follows the repository's
token-restoration discipline, so an echo registration nests and detaches like
every other one.

See :mod:`cuprum.echo_events` for the event types this channel carries.
"""

from __future__ import annotations

import inspect
import logging
import typing as typ
from contextvars import ContextVar

if typ.TYPE_CHECKING:
    from cuprum.echo_events import EchoEvent, EchoHook

if typ.TYPE_CHECKING:
    from contextvars import Token

_LOGGER = logging.getLogger(__name__)

_echo_hooks: ContextVar[tuple[EchoHook, ...]] = ContextVar(
    "cuprum_echo_hooks",
    default=(),
)


class EchoHookRegistration:
    """Registration handle for a stream-echo observation hook.

    Supports ``detach()`` and context-manager use. The handle captures a
    :class:`~contextvars.Token` when it installs the extended hook tuple, and
    :meth:`detach` restores the exact tuple that was current when the handle
    was created — the same discipline
    :class:`cuprum.pump_observation.PumpHookRegistration` applies. Detach in the
    :class:`~contextvars.Context` that created the registration; resetting a
    ``ContextVar`` with a token from another context raises :class:`ValueError`.

    Prefer ``with`` blocks, which detach in last-in-first-out order; detaching
    out of order restores a tuple that discards later registrations.
    """

    __slots__ = ("_detached", "_hook", "_token")

    def __init__(self, hook: EchoHook) -> None:
        """Append ``hook`` to the current context's echo hooks."""
        self._hook = hook
        self._detached = False
        self._token: Token[tuple[EchoHook, ...]] = _echo_hooks.set((
            *_echo_hooks.get(),
            hook,
        ))

    def detach(self) -> None:
        """Restore the echo hooks that preceded this registration."""
        if self._detached:
            return
        _echo_hooks.reset(self._token)
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


def observe_echo(hook: EchoHook) -> EchoHookRegistration:
    """Register a hook for stream-echo failure events in the current context.

    Parameters
    ----------
    hook:
        Synchronous callable invoked with each
        :class:`~cuprum.echo_events.EchoEvent`.

    Returns
    -------
    EchoHookRegistration
        A handle that can be detached or used as a context manager.

    Examples
    --------
    Count echo failures into a metrics collector::

        from cuprum.adapters.metrics_adapter import InMemoryMetrics
        from cuprum.adapters.echo_metrics import EchoMetricsHook

        metrics = InMemoryMetrics()
        with observe_echo(EchoMetricsHook(metrics)):
            pipeline.run_sync()

    """
    return EchoHookRegistration(hook)


def current_echo_hooks() -> tuple[EchoHook, ...]:
    """Return the echo hooks registered in the current context.

    Returns
    -------
    tuple[EchoHook, ...]
        The registered hooks in registration order, empty when none are
        registered.

    """
    return _echo_hooks.get()


def _emit_echo_event(event: EchoEvent) -> None:
    """Deliver ``event`` to every registered echo hook.

    Hook-failure policy
    -------------------
    A hook that raises :class:`Exception` is reported at ``WARNING`` with its
    traceback and the remaining hooks still run, mirroring
    :func:`cuprum.pump_observation._emit_pump_event`. Both emission sites sit on
    paths whose contract is that they must complete: propagating would let
    registering a metrics backend turn a run that would have captured its
    output into one that fails — a change in execution behaviour caused by
    observing it. The failure is recorded, not swallowed.

    Anything that is not an :class:`Exception` — ``SystemExit``,
    ``KeyboardInterrupt``, ``asyncio.CancelledError`` — propagates untouched.

    With no hooks registered this returns before touching the event, so a
    caller that has not opted in pays nothing and behaves exactly as before.
    """
    hooks = _echo_hooks.get()
    if not hooks:
        return
    for hook in hooks:
        _invoke_echo_hook(hook, event)


def _invoke_echo_hook(hook: EchoHook, event: EchoEvent) -> None:
    """Invoke one echo hook, reporting rather than propagating its failure."""
    try:
        result = hook(event)
    except Exception as exc:
        _LOGGER.warning(
            "echo_observer_failed stream=%s error=%s",
            event.stream,
            type(exc).__name__,
            exc_info=True,
            extra={
                "cuprum_action": "echo_observer_failed",
                "cuprum_stream": str(event.stream),
                "cuprum_error_type": type(exc).__name__,
            },
        )
        return
    if result is not None:
        _discard_echo_hook_result(result, event)


def _discard_echo_hook_result(result: object, event: EchoEvent) -> None:
    """Report a non-``None`` hook return, closing coroutines it left behind.

    Echo hooks are synchronous by contract. An unawaited coroutine would
    otherwise surface later as an unrelated ``coroutine was never awaited``
    warning, so it is closed here and reported against the event that produced
    it.
    """
    _LOGGER.warning(
        "echo_observer_returned_value stream=%s result_type=%s",
        event.stream,
        type(result).__name__,
        extra={
            "cuprum_action": "echo_observer_returned_value",
            "cuprum_stream": str(event.stream),
            "cuprum_result_type": type(result).__name__,
        },
    )
    if inspect.iscoroutine(result):
        result.close()


__all__ = [
    "EchoHookRegistration",
    "current_echo_hooks",
    "observe_echo",
]
