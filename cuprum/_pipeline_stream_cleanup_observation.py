"""Observability emissions for native pipeline-pump cleanup."""

from __future__ import annotations

import typing as typ

from cuprum.pump_events import PumpEvent, RustPumpDeclineReason
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


def _log_native_pump_declined(
    logger: logging.Logger,
    reason: RustPumpDeclineReason,
) -> None:
    """Record the reason an inter-stage hop falls back to Python pumping."""
    logger.debug(
        "Inter-stage hop declined the Rust pump (%s); using the Python pump",
        reason.value,
        extra={"cuprum_action": "rust_pump_declined", "cuprum_reason": reason.value},
    )
    _emit_pump_event(PumpEvent(phase="declined", reason=reason))


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


def _log_native_pump_handoff_failed(
    logger: logging.Logger,
    phase: typ.Literal["duplicate_writer", "executor_submission"],
    error: BaseException,
) -> None:
    """Record a failure before Rust owns the duplicate writer."""
    logger.debug(
        "Rust pump hand-off failed during %s",
        phase,
        extra={
            "cuprum_action": "rust_pump_handoff_failed",
            "cuprum_phase": phase,
            "cuprum_outcome": "failed",
            "cuprum_error_type": type(error).__name__,
            "cuprum_errno": error.errno if isinstance(error, OSError) else None,
        },
    )


def _log_native_pump_failed_after_cancel(
    logger: logging.Logger,
    error: BaseException,
) -> None:
    """Record a native-pump failure masked by caller-requested cancellation."""
    logger.debug(
        "Rust pump failed while its hop was being cancelled",
        exc_info=error,
        extra={"cuprum_action": "rust_pump_failed_after_cancel"},
    )
    _emit_pump_event(PumpEvent(phase="failed_after_cancel"))
