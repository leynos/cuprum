"""Registration and fail-open emission for line-stream lifecycle events.

Examples
--------
Register a scoped observer while iterating a configured command::

    events = []
    with observe_line_stream(events.append):
        async with command.lines() as stream:
            async for event in stream:
                consume(event)
"""

from __future__ import annotations

import inspect
import logging
from contextvars import ContextVar

from cuprum._scope_registration import _IdentityTupleRegistration
from cuprum.line_stream_events import LineStreamEvent, LineStreamHook

_LOGGER = logging.getLogger(__name__)

_line_stream_hooks: ContextVar[tuple[LineStreamHook, ...]] = ContextVar(
    "cuprum_line_stream_hooks",
    default=(),
)


class LineStreamHookRegistration(_IdentityTupleRegistration[LineStreamHook]):
    """Scoped registration handle for one line-stream lifecycle hook.

    Notes
    -----
    Entering the registration leaves its hook active in the current context.
    Exiting the scope or calling :meth:`detach` removes exactly the hook this
    handle registered, so detaching out of last-in-first-out order cannot
    resurrect a stale hook that was already removed. That guarantee is why this
    handle removes its own entry by identity instead of restoring a token; see
    :class:`~cuprum._scope_registration._IdentityTupleRegistration`.
    """

    __slots__ = ()

    def __init__(self, hook: LineStreamHook) -> None:
        """Append ``hook`` to the current line-stream observation scope."""
        super().__init__(_line_stream_hooks, hook)


def observe_line_stream(hook: LineStreamHook) -> LineStreamHookRegistration:
    """Register a synchronous lifecycle observer for ``SafeCmd.lines()``.

    Observer failures are logged and do not alter subprocess results, timeout
    translation, or cancellation. Prefer a ``with`` block so registration is
    restored in the same context that installed it.

    Parameters
    ----------
    hook
        A synchronous callable that receives each :class:`LineStreamEvent` in
        the active context.

    Returns
    -------
    LineStreamHookRegistration
        A scoped registration that detaches the hook on scope exit or explicit
        :meth:`LineStreamHookRegistration.detach`.
    """
    return LineStreamHookRegistration(hook)


def _emit_line_stream_event(event: LineStreamEvent) -> None:
    """Log and deliver one lifecycle event without affecting command semantics."""
    _LOGGER.debug(
        "line_stream_event phase=%s stream=%s sink=%s",
        event.phase,
        event.stream,
        event.sink,
        extra={
            "cuprum_action": "line_stream_event",
            "cuprum_phase": event.phase,
            "cuprum_exec_id": event.exec_id,
            "cuprum_pid": event.pid,
            "cuprum_stream": event.stream,
            "cuprum_sink": event.sink,
            "cuprum_queue_size": event.queue_size,
            "cuprum_queue_capacity": event.queue_capacity,
            "cuprum_error_type": event.error_type,
        },
    )
    for hook in _line_stream_hooks.get():
        _invoke_line_stream_hook(hook, event)


def _invoke_line_stream_hook(hook: LineStreamHook, event: LineStreamEvent) -> None:
    """Invoke one hook, logging ordinary observer failures and continuing."""
    try:
        outcome = hook(event)
    except Exception as exc:
        _LOGGER.exception(
            "line_stream_observer_failed phase=%s",
            event.phase,
            extra={
                "cuprum_action": "line_stream_observer_failed",
                "cuprum_phase": event.phase,
                "cuprum_error_type": type(exc).__name__,
            },
        )
        return
    if outcome is not None:
        _LOGGER.warning(
            "line_stream_observer_returned_value phase=%s result_type=%s",
            event.phase,
            type(outcome).__name__,
            extra={
                "cuprum_action": "line_stream_observer_returned_value",
                "cuprum_phase": event.phase,
                "cuprum_result_type": type(outcome).__name__,
            },
        )
        if inspect.iscoroutine(outcome):
            outcome.close()


__all__ = ["LineStreamHookRegistration", "observe_line_stream"]
