"""Registration and fail-open emission for aggregate stream-operation events."""

from __future__ import annotations

import dataclasses as dc
import inspect
import logging
import time
import typing as typ
from contextvars import ContextVar

from cuprum.stream_events import (
    StreamOperation,
    StreamOperationEvent,
    StreamOperationHook,
    StreamOperationOutcome,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecId

_LOGGER = logging.getLogger(__name__)
_stream_operation_hooks: ContextVar[tuple[StreamOperationHook, ...]] = ContextVar(
    "cuprum_stream_operation_hooks",
    default=(),
)


@dc.dataclass(slots=True)
class _StreamOperationMeasurement:
    """Aggregate reader results until one stream operation completes."""

    operation: StreamOperation
    hooks: tuple[StreamOperationHook, ...]
    monotonic_clock: cabc.Callable[[], float]
    started_s: float
    exec_id: ExecId | None
    bytes_consumed: int = 0
    read_operations: int = 0

    def record_read(self, chunk: bytes) -> None:
        """Accumulate one completed reader result, including EOF."""
        self.read_operations += 1
        self.bytes_consumed += len(chunk)

    def complete(self, outcome: StreamOperationOutcome) -> None:
        """Emit the single aggregate completion event for this operation."""
        _emit_stream_operation_event(
            StreamOperationEvent(
                operation=self.operation,
                outcome=outcome,
                bytes_consumed=self.bytes_consumed,
                read_operations=self.read_operations,
                duration_s=self.monotonic_clock() - self.started_s,
                exec_id=self.exec_id,
            ),
            hooks=self.hooks,
        )


def current_stream_operation_hooks() -> tuple[StreamOperationHook, ...]:
    """Return stream-operation hooks registered in the current context."""
    return _stream_operation_hooks.get()


class StreamOperationHookRegistration:
    """Registration handle for a stream-operation observation hook."""

    __slots__ = ("_detached", "_hook")

    def __init__(self, hook: StreamOperationHook) -> None:
        """Append ``hook`` to the current context's stream-operation hooks."""
        self._detached = False
        self._hook = hook
        _stream_operation_hooks.set((*_stream_operation_hooks.get(), hook))

    def detach(self) -> None:
        """Remove this registration without restoring stale hook state."""
        if self._detached:
            return
        hooks = list(_stream_operation_hooks.get())
        for index in range(len(hooks) - 1, -1, -1):
            if hooks[index] is self._hook:
                del hooks[index]
                break
        _stream_operation_hooks.set(tuple(hooks))
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


def observe_stream_operation(
    hook: StreamOperationHook,
) -> StreamOperationHookRegistration:
    """Register a hook for aggregate stream-operation completion events.

    Parameters
    ----------
    hook:
        Synchronous callable invoked once for each completed registered stream
        operation.

    Returns
    -------
    StreamOperationHookRegistration
        A context-local registration handle that supports ``detach()`` and
        context-manager use.

    """
    return StreamOperationHookRegistration(hook)


def _start_stream_operation(
    operation: StreamOperation,
    *,
    exec_id: ExecId | None = None,
    monotonic_clock: cabc.Callable[[], float] = time.monotonic,
) -> _StreamOperationMeasurement | None:
    """Start aggregate measurement only when a hook has opted in."""
    hooks = current_stream_operation_hooks()
    if not hooks:
        return None
    return _StreamOperationMeasurement(
        operation=operation,
        hooks=hooks,
        monotonic_clock=monotonic_clock,
        started_s=monotonic_clock(),
        exec_id=exec_id,
    )


def _complete_stream_operation(
    measurement: _StreamOperationMeasurement | None,
    outcome: StreamOperationOutcome,
) -> None:
    """Emit completion when the operation started under an observer."""
    if measurement is not None:
        measurement.complete(outcome)


def _record_stream_read(
    measurement: _StreamOperationMeasurement | None,
    chunk: bytes,
) -> None:
    """Count one completed reader result when aggregate observation is active."""
    if measurement is not None:
        measurement.record_read(chunk)


def _emit_stream_operation_event(
    event: StreamOperationEvent,
    *,
    hooks: tuple[StreamOperationHook, ...],
) -> None:
    """Deliver an event without allowing observer failures to affect streams."""
    for hook in hooks:
        try:
            result = hook(event)
        except Exception as exc:
            _LOGGER.warning(
                "stream_operation_observer_failed operation=%s outcome=%s error=%s",
                event.operation,
                event.outcome,
                type(exc).__name__,
                exc_info=True,
                extra={
                    "cuprum_action": "stream_operation_observer_failed",
                    "cuprum_operation": str(event.operation),
                    "cuprum_outcome": str(event.outcome),
                    "cuprum_error_type": type(exc).__name__,
                },
            )
            continue
        if result is not None:
            _discard_hook_result(result, event)


def _discard_hook_result(result: object, event: StreamOperationEvent) -> None:
    """Report non-``None`` hook returns and close accidental coroutines."""
    _LOGGER.warning(
        "stream_operation_observer_returned_value operation=%s result_type=%s",
        event.operation,
        type(result).__name__,
        extra={
            "cuprum_action": "stream_operation_observer_returned_value",
            "cuprum_operation": str(event.operation),
            "cuprum_result_type": type(result).__name__,
        },
    )
    if inspect.iscoroutine(result):
        result.close()


__all__ = [
    "StreamOperationHookRegistration",
    "current_stream_operation_hooks",
    "observe_stream_operation",
]
