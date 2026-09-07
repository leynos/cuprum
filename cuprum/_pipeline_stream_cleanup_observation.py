"""Observability emissions for native pipeline-pump cleanup."""
from __future__ import annotations

import asyncio
import logging
import typing as typ

from cuprum.pump_events import PumpEvent, RustPumpDeclineReason
from cuprum.pump_observation import _current_pump_event_exec_id, _emit_pump_event
import dataclasses as dc

if typ.TYPE_CHECKING:
    import collections.abc as cabc
_LOGGER = logging.getLogger(__name__)


def _log_native_pump_cleanup_started(logger: logging.Logger) -> None:
    """Record that cancellation is waiting for native-pump cleanup."""
    event = PumpEvent(
        phase="cleanup_started",
        exec_id=_current_pump_event_exec_id(),
    )
    _emit_native_pump_cleanup_observation(
        logger,
        event,
        _NativePumpCleanupLog(
            "Native pump cleanup started after cancellation",
            (),
            "started",
            {},
        ),
    )


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
    _emit_native_pump_cleanup_observation(
        logger,
        event,
        _NativePumpCleanupLog(
            "Native pump cleanup completed after cancellation in %.6fs",
            (duration_s,),
            "completed",
            {"cuprum_duration_s": duration_s},
        ),
    )


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
    _emit_native_pump_cleanup_observation(
        logger,
        event,
        _NativePumpCleanupLog(
            "Native pump cleanup grace expired after %.6fs",
            (elapsed_s,),
            "grace_expired",
            {"cuprum_elapsed_s": elapsed_s},
        ),
    )


def _log_native_pump_cleanup_deferred(logger: logging.Logger) -> None:
    """Record completion of cleanup deferred beyond the caller grace."""
    event = PumpEvent(
        phase="cleanup_deferred",
        exec_id=_current_pump_event_exec_id(),
    )
    _emit_native_pump_cleanup_observation(
        logger,
        event,
        _NativePumpCleanupLog(
            "Native pump deferred cleanup completed",
            (),
            "deferred",
            {},
        ),
    )


async def _await_native_pump_cleanup(
    cleanup_complete: asyncio.Future[None],
    *,
    monotonic_clock: cabc.Callable[[], float],
    logger: logging.Logger | None = None,
) -> None:
    """Wait for worker cleanup despite repeated cancellation requests."""
    cleanup_logger = logger or _LOGGER
    started_at = monotonic_clock()
    _log_native_pump_cleanup_started(cleanup_logger)
    try:
        while not cleanup_complete.done():
            try:
                await asyncio.shield(cleanup_complete)
            except asyncio.CancelledError:
                continue
    finally:
        if cleanup_complete.done():
            _log_native_pump_cleanup_completed(
                cleanup_logger,
                monotonic_clock() - started_at,
            )


def _log_native_pump_handoff_failed(
    logger: logging.Logger,
    phase: typ.Literal[
        "duplicate_writer",
        "executor_submission",
        "reader_preparation",
        "platform_writer_transfer",
    ],
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

@dc.dataclass(frozen=True, slots=True)
class _NativePumpCleanupLog:
    """Phase-specific fields used to record one cleanup observation."""

    message: str
    formatting_args: tuple[object, ...]
    outcome: str
    phase_fields: dict[str, object]

def _emit_native_pump_cleanup_observation(
    logger: logging.Logger,
    event: PumpEvent,
    log: _NativePumpCleanupLog,
) -> None:
    """Log and emit one native-pump cleanup observation."""
    extra = {
        "cuprum_action": "rust_pump_cleanup",
        "cuprum_operation": "native_pump_cleanup",
        "cuprum_outcome": log.outcome,
    }
    extra.update(log.phase_fields)
    logger.debug(log.message, *log.formatting_args, extra=extra)
    _emit_pump_event(event)
