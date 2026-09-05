"""Observability emissions for native pipeline-pump cleanup."""

from __future__ import annotations

import typing as typ

from cuprum.pump_events import PumpEvent
from cuprum.pump_observation import _current_pump_event_exec_id, _emit_pump_event

if typ.TYPE_CHECKING:
    import logging


def _log_native_pump_cleanup_started(logger: logging.Logger) -> None:
    """Record that cancellation is waiting for native-pump cleanup."""
    event = PumpEvent(
        phase="cleanup_started",
        exec_id=_current_pump_event_exec_id(),
    )
    logger.debug(
        "Native pump cleanup started after cancellation",
        extra={
            "cuprum_action": "rust_pump_cleanup",
            "cuprum_operation": "native_pump_cleanup",
            "cuprum_outcome": "started",
        },
    )
    _emit_pump_event(event)


def _log_native_pump_cleanup_completed(
    logger: logging.Logger,
    duration_s: float,
) -> None:
    """Record that native-pump cleanup released its descriptors."""
    event = PumpEvent(
        phase="cleanup_completed",
        duration_s=duration_s,
        exec_id=_current_pump_event_exec_id(),
    )
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
    _emit_pump_event(event)


def _log_native_pump_cleanup_grace_expired(
    logger: logging.Logger,
    elapsed_s: float,
) -> None:
    """Record that caller-facing native-pump cleanup reached its grace limit."""
    event = PumpEvent(
        phase="cleanup_grace_expired",
        elapsed_s=elapsed_s,
        exec_id=_current_pump_event_exec_id(),
    )
    logger.debug(
        "Native pump cleanup grace expired after %.6fs",
        elapsed_s,
        extra={
            "cuprum_action": "rust_pump_cleanup",
            "cuprum_operation": "native_pump_cleanup",
            "cuprum_outcome": "grace_expired",
            "cuprum_elapsed_s": elapsed_s,
        },
    )
    _emit_pump_event(event)


def _log_native_pump_cleanup_deferred(logger: logging.Logger) -> None:
    """Record completion of cleanup deferred beyond the caller grace."""
    event = PumpEvent(
        phase="cleanup_deferred",
        exec_id=_current_pump_event_exec_id(),
    )
    logger.debug(
        "Native pump deferred cleanup completed",
        extra={
            "cuprum_action": "rust_pump_cleanup",
            "cuprum_operation": "native_pump_cleanup",
            "cuprum_outcome": "deferred",
        },
    )
    _emit_pump_event(event)
