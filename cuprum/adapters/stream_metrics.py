"""Bounded aggregate metrics for completed pure-Python stream operations."""

from __future__ import annotations

import typing as typ

from cuprum.stream_events import (
    StreamOperation,
    StreamOperationEvent,
    StreamOperationHook,
    StreamOperationOutcome,
)

if typ.TYPE_CHECKING:
    from cuprum.adapters.metrics_adapter import MetricsCollector

STREAM_OPERATION_BYTES_TOTAL = "cuprum_stream_operation_bytes_total"
STREAM_OPERATION_READ_OPERATIONS_TOTAL = "cuprum_stream_operation_read_operations_total"
STREAM_OPERATION_DURATION_SECONDS = "cuprum_stream_operation_duration_seconds"


class StreamOperationMetricsHook:
    """Project aggregate stream-operation events into bounded metrics.

    The hook emits one byte counter, one reader-operation counter, and one
    duration histogram for every received event. Every metric uses exactly the
    closed ``operation`` and ``outcome`` labels from the event enums.
    """

    __slots__ = ("_collector",)

    def __init__(self, collector: MetricsCollector) -> None:
        """Initialize the hook with a thread-safe metrics collector."""
        self._collector = collector

    def __call__(self, event: StreamOperationEvent) -> None:
        """Record all aggregate metrics for a recognized closed event."""
        labels = _stream_operation_labels(event)
        if labels is None:
            return
        self._collector.inc_counter(
            STREAM_OPERATION_BYTES_TOTAL,
            float(event.bytes_consumed),
            labels,
        )
        self._collector.inc_counter(
            STREAM_OPERATION_READ_OPERATIONS_TOTAL,
            float(event.read_operations),
            labels,
        )
        self._collector.observe_histogram(
            STREAM_OPERATION_DURATION_SECONDS,
            event.duration_s,
            labels,
        )


def _stream_operation_labels(event: StreamOperationEvent) -> dict[str, str] | None:
    """Return labels only when both caller-constructible fields are closed."""
    if not isinstance(event.operation, StreamOperation) or not isinstance(
        event.outcome,
        StreamOperationOutcome,
    ):
        return None
    return {"operation": str(event.operation), "outcome": str(event.outcome)}


def stream_operation_metrics_hook(collector: MetricsCollector) -> StreamOperationHook:
    """Create a bounded metrics hook for stream-operation observation."""
    return StreamOperationMetricsHook(collector)


__all__ = [
    "STREAM_OPERATION_BYTES_TOTAL",
    "STREAM_OPERATION_DURATION_SECONDS",
    "STREAM_OPERATION_READ_OPERATIONS_TOTAL",
    "StreamOperationMetricsHook",
    "stream_operation_metrics_hook",
]
