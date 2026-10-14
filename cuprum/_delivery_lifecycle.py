"""Close run-owned output workers and publish their loop-side diagnostics."""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._bounded_delivery import (
    _DELIVERY_DRAIN_GRACE_S,
    _DeliveryPayload,
    _DeliveryWorker,
)
from cuprum.echo_events import (
    EchoErrorCategory,
    EchoEvent,
    EchoStream,
    RelayFallback,
)
from cuprum.echo_observation import _emit_echo_event

if typ.TYPE_CHECKING:
    from cuprum._stream_drain_state import _RelayDiagnostics


async def _close_delivery_worker(
    worker: _DeliveryWorker | None,
    *,
    drain_grace_s: float | None = None,
) -> tuple[RelayFallback, ...]:
    """Close one worker off-loop and publish each deferred diagnostic once."""
    if worker is None:
        return ()
    grace = _DELIVERY_DRAIN_GRACE_S if drain_grace_s is None else drain_grace_s
    await asyncio.to_thread(worker.close, grace)
    result = worker.take_close_result()
    if result is None:
        return ()
    fallbacks = [
        _record_delivery_event(
            failure.stream,
            EchoErrorCategory.DELIVERY_ERROR,
            diagnostics=failure.diagnostics,
        )
        for failure in result.failures
    ]
    fallbacks.extend(
        _record_delivery_event(
            pending.stream,
            EchoErrorCategory.UNDELIVERED,
            dropped_bytes=pending.byte_count,
            diagnostics=pending.diagnostics,
        )
        for pending in result.undelivered_bytes
    )
    return tuple(fallbacks)


def _queue_idle_diagnostic(
    worker: _DeliveryWorker,
    diagnostics: _RelayDiagnostics,
    line: str,
) -> None:
    """Queue one generated idle line through the run's stderr FIFO."""
    dropped_bytes = worker.enqueue(
        EchoStream.STDERR,
        _DeliveryPayload(value=line, source_bytes=len(line.encode("ascii"))),
        flush=True,
        diagnostics=diagnostics,
    )
    if dropped_bytes:
        _record_delivery_event(
            EchoStream.STDERR,
            EchoErrorCategory.DROPPED,
            dropped_bytes=dropped_bytes,
            diagnostics=diagnostics,
        )


def _record_delivery_event(
    stream: EchoStream,
    category: EchoErrorCategory,
    *,
    dropped_bytes: int | None = None,
    diagnostics: _RelayDiagnostics | None = None,
) -> RelayFallback:
    """Record categorical delivery loss on the loop-owned diagnostics path."""
    fallback = RelayFallback(stream=stream, error_category=category)
    if diagnostics is not None:
        diagnostics.fallbacks.append(fallback)
    _emit_echo_event(
        EchoEvent(
            stream=stream,
            error_category=category,
            dropped_bytes=dropped_bytes,
        ),
    )
    return fallback


__all__ = ["_close_delivery_worker", "_queue_idle_diagnostic"]
