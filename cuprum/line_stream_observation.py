"""Registration and fail-open emission for line-stream lifecycle events."""

from __future__ import annotations

import inspect
import logging
import typing as typ
from contextvars import ContextVar

if typ.TYPE_CHECKING:
    from contextvars import Token

    from cuprum.line_stream_events import LineStreamEvent, LineStreamHook

_LOGGER = logging.getLogger(__name__)

_line_stream_hooks: ContextVar[tuple[LineStreamHook, ...]] = ContextVar(
    "cuprum_line_stream_hooks",
    default=(),
)


class LineStreamHookRegistration:
    """Scoped registration handle for one line-stream lifecycle hook."""

    __slots__ = ("_detached", "_token")

    def __init__(self, hook: LineStreamHook) -> None:
        """Append ``hook`` to the current line-stream observation scope."""
        self._detached = False
        self._token: Token[tuple[LineStreamHook, ...]] = _line_stream_hooks.set((
            *_line_stream_hooks.get(),
            hook,
        ))

    def detach(self) -> None:
        """Restore the hook collection active before this registration."""
        if not self._detached:
            _line_stream_hooks.reset(self._token)
            self._detached = True

    def __enter__(self) -> typ.Self:
        """Enter the already-registered observation scope."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Detach the hook as the observation scope exits."""
        self.detach()


def observe_line_stream(hook: LineStreamHook) -> LineStreamHookRegistration:
    """Register a synchronous lifecycle observer for ``SafeCmd.lines()``.

    Observer failures are logged and do not alter subprocess results, timeout
    translation, or cancellation. Prefer a ``with`` block so registration is
    restored in the same context that installed it.

    Returns
    -------
    LineStreamHookRegistration
        A scoped registration that detaches the hook.
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
        _LOGGER.warning(
            "line_stream_observer_failed phase=%s error=%s",
            event.phase,
            type(exc).__name__,
            exc_info=True,
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
