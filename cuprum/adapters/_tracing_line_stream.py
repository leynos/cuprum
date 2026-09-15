"""Trace-event projection for correlated line-stream lifecycle events."""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from cuprum.adapters.tracing_adapter import TracingHook
    from cuprum.line_stream_events import LineStreamEvent


class _LineStreamTracingMixin:
    """Add line-stream lifecycle events to an open execution span."""

    def record_line_stream_event(self, event: LineStreamEvent) -> None:
        """Record one correlated lifecycle event when the execution span is open."""
        hook = typ.cast("TracingHook", self)
        with hook._lock:
            active = hook._span_states.get(event.exec_id)
            if active is not None:
                hook._active_spans.move_to_end(event.exec_id)
        if active is None:
            return
        attributes = _line_stream_attributes(event)
        with active.lock:
            if not active.is_closed:
                active.span.add_event("cuprum.line_stream", attributes)


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
