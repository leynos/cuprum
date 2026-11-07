"""Trace-event projection for correlated line-stream lifecycle events."""

from __future__ import annotations

import typing as typ

from cuprum.adapters._tracing_span_events import record_span_event

if typ.TYPE_CHECKING:
    from cuprum.adapters.tracing_adapter import TracingHook
    from cuprum.line_stream_events import LineStreamEvent


class _LineStreamTracingMixin:
    """Add line-stream lifecycle events to an open execution span."""

    def record_line_stream_event(self, event: LineStreamEvent) -> None:
        """Record one correlated lifecycle event when the execution span is open."""
        record_span_event(
            typ.cast("TracingHook", self),
            event.exec_id,
            "cuprum.line_stream",
            _line_stream_attributes(event),
        )


def _line_stream_attributes(event: LineStreamEvent) -> dict[str, object]:
    """Build the bounded line-stream projection attached to an execution span."""
    attributes: dict[str, object] = {"phase": event.phase}
    attributes.update({
        name: value
        for name, value in (
            ("pid", event.pid),
            ("stream", event.stream),
            ("sink", event.sink),
            ("queue_size", event.queue_size),
            ("queue_capacity", event.queue_capacity),
            ("error_type", event.error_type),
        )
        if value is not None
    })
    return attributes
