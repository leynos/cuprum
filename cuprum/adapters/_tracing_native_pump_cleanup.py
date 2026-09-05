"""Trace-event projection for correlated native-pump cleanup."""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from cuprum.adapters.tracing_adapter import TracingHook
    from cuprum.pump_events import PumpEvent


class _NativePumpCleanupTracingMixin:
    """Add pump-channel cleanup events to an existing execution span."""

    @staticmethod
    def _build_cleanup_attributes(event: PumpEvent) -> dict[str, object]:
        """Build the bounded tracing attributes for a cleanup event."""
        attributes: dict[str, object] = {
            "operation": "native_pump_cleanup",
            "outcome": event.phase.removeprefix("cleanup_"),
        }
        if event.phase == "cleanup_completed" and event.duration_s is not None:
            attributes["duration_s"] = event.duration_s
        if event.phase == "cleanup_grace_expired" and event.elapsed_s is not None:
            attributes["elapsed_s"] = event.elapsed_s
        return attributes

    def record_pump_event(self, event: PumpEvent) -> None:
        """Record a correlated native-pump cleanup event when its span is open.

        Parameters
        ----------
        event:
            A pump-channel event. Only correlated cleanup lifecycle events are
            recorded; every other event is ignored.

        """
        if event.phase not in {
            "cleanup_started",
            "cleanup_completed",
            "cleanup_grace_expired",
            "cleanup_deferred",
        }:
            return
        if event.exec_id is None:
            return

        hook = typ.cast("TracingHook", self)
        with hook._lock:
            active = hook._span_states.get(event.exec_id)
            if active is not None:
                hook._active_spans.move_to_end(event.exec_id)
        if active is None:
            return

        attributes = self._build_cleanup_attributes(event)
        with active.lock:
            if not active.is_closed:
                active.span.add_event(
                    f"cuprum.{event.phase}",
                    attributes,
                )
