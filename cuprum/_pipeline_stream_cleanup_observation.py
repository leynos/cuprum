"""Observability emissions for native pipeline-pump cleanup."""

from __future__ import annotations

import typing as typ

from cuprum.pump_events import PumpEvent
from cuprum.pump_observation import _emit_pump_event

if typ.TYPE_CHECKING:
    import logging


def _log_native_pump_cleanup_started(logger: logging.Logger) -> None:
    """Record that cancellation is waiting for native-pump cleanup."""
    logger.debug(
        "Native pump cleanup started after cancellation",
        extra={
            "cuprum_action": "rust_pump_cleanup",
            "cuprum_operation": "native_pump_cleanup",
            "cuprum_outcome": "started",
        },
    )
    _emit_pump_event(PumpEvent(phase="cleanup_started"))


def _log_native_pump_cleanup_completed(
    logger: logging.Logger,
    duration_s: float,
) -> None:
    """Record that native-pump cleanup released its descriptors."""
    logger.debug(
        "Native pump cleanup completed after cancellation in %.6fs",
        duration_s,
        extra={
            "cuprum_action": "rust_pump_cleanup",
            "cuprum_operation": "native_pump_cleanup",
            "cuprum_outcome": "completed",
            "cuprum_duration_s": duration_s,
        },
    )
    _emit_pump_event(PumpEvent(phase="cleanup_completed", duration_s=duration_s))
