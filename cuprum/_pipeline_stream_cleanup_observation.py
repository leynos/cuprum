"""Observability emissions for native pipeline-pump cleanup."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import logging
import types
import typing as typ

from cuprum.pump_events import PumpEvent, RustPumpDeclineReason
from cuprum.pump_observation import _current_pump_event_exec_id, _emit_pump_event

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_LOGGER = logging.getLogger(__name__)


@dc.dataclass(frozen=True, slots=True)
class _NativePumpCleanupLog:
    """Phase-specific fields used to record one cleanup observation."""

    message: str
    formatting_args: tuple[object, ...]
    outcome: str
    phase_fields: dict[str, object]


@dc.dataclass(frozen=True, slots=True)
class _NativePumpCleanupMetadata:
    """Describe the event and DEBUG timing fields for one cleanup phase."""

    message: str
    outcome: str
    timing_field: typ.Literal["duration_s", "elapsed_s"] | None
    debug_timing_field: str | None


_NATIVE_PUMP_CLEANUP_LOGS: cabc.Mapping[str, _NativePumpCleanupMetadata] = (
    types.MappingProxyType({
        "cleanup_started": _NativePumpCleanupMetadata(
            "Native pump cleanup started after cancellation",
            "started",
            None,
            None,
        ),
        "cleanup_completed": _NativePumpCleanupMetadata(
            "Native pump cleanup completed after cancellation in %.6fs",
            "completed",
            "duration_s",
            "cuprum_duration_s",
        ),
        "cleanup_grace_expired": _NativePumpCleanupMetadata(
            "Native pump cleanup grace expired after %.6fs",
            "grace_expired",
            "elapsed_s",
            "cuprum_elapsed_s",
        ),
        "cleanup_deferred": _NativePumpCleanupMetadata(
            "Native pump deferred cleanup completed",
            "deferred",
            None,
            None,
        ),
    })
)
"""The closed metadata vocabulary for native-pump cleanup observations."""


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


def _log_native_pump_cleanup(
    logger: logging.Logger,
    phase: typ.Literal[
        "cleanup_started",
        "cleanup_completed",
        "cleanup_grace_expired",
        "cleanup_deferred",
    ],
    timing_s: float | None = None,
) -> None:
    """Record one native-pump cleanup lifecycle phase."""
    metadata = _NATIVE_PUMP_CLEANUP_LOGS[phase]
    event = PumpEvent(
        phase=phase,
        duration_s=timing_s if metadata.timing_field == "duration_s" else None,
        elapsed_s=timing_s if metadata.timing_field == "elapsed_s" else None,
        exec_id=_current_pump_event_exec_id(),
    )
    formatting_args: tuple[object, ...] = (
        (timing_s,) if metadata.timing_field is not None else ()
    )
    phase_fields: dict[str, object] = (
        {metadata.debug_timing_field: timing_s}
        if metadata.debug_timing_field is not None
        else {}
    )
    _emit_native_pump_cleanup_observation(
        logger,
        event,
        _NativePumpCleanupLog(
            metadata.message,
            formatting_args,
            metadata.outcome,
            phase_fields,
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
class _NativePumpCleanupWait:
    """Caller-supplied policy for one deferred cleanup wait."""

    logger: logging.Logger
    monotonic_clock: cabc.Callable[[], float]
    cleanup_grace_s: float
    state: object | None


def _defer_native_pump_cleanup(
    *,
    wait: _NativePumpCleanupWait,
    started_at: float,
) -> None:
    """Mark callback-owned cleanup deferred and report its caller-bound expiry."""
    if wait.state is not None:
        wait.state.was_deferred = True
    _log_native_pump_cleanup(
        wait.logger,
        "cleanup_grace_expired",
        max(0.0, wait.monotonic_clock() - started_at),
    )


async def _await_native_pump_cleanup(
    cleanup_complete: asyncio.Future[None],
    *,
    wait: _NativePumpCleanupWait,
) -> None:
    """Wait for cleanup or defer it when its caller grace expires."""
    started_at = wait.monotonic_clock()
    deadline = started_at + wait.cleanup_grace_s
    _log_native_pump_cleanup(wait.logger, "cleanup_started")
    try:
        while not cleanup_complete.done():
            remaining_s = deadline - wait.monotonic_clock()
            if remaining_s <= 0:
                _defer_native_pump_cleanup(
                    wait=wait,
                    started_at=started_at,
                )
                return
            try:
                async with asyncio.timeout(remaining_s):
                    await asyncio.shield(cleanup_complete)
            except asyncio.CancelledError:
                continue
            except TimeoutError:
                if not cleanup_complete.done():
                    _defer_native_pump_cleanup(
                        wait=wait,
                        started_at=started_at,
                    )
                    return
    finally:
        if cleanup_complete.done():
            _log_native_pump_cleanup(
                wait.logger,
                "cleanup_completed",
                wait.monotonic_clock() - started_at,
            )
