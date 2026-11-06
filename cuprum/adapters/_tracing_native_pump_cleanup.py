"""Trace-event projection for correlated native-pump cleanup."""

from __future__ import annotations

import typing as typ

from cuprum.adapters._tracing_span_events import record_span_event

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

        record_span_event(
            typ.cast("TracingHook", self),
            event.exec_id,
            f"cuprum.{event.phase}",
            self._build_cleanup_attributes(event),
        )
