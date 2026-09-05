"""Resources retained until a Rust-pump executor future settles."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum import _pipeline_stream_cleanup_observation as _pump_obs
from cuprum._pipeline_stream_fds import _suppressed_teardown_failure
from cuprum.pump_span_observation import _close_pump_hop_spans

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc
    import logging

    from cuprum.pump_span_observation import _PumpHopSpans


@dc.dataclass(frozen=True, slots=True)
class _RustPumpCompletion:
    """Resources whose lifetime ends when the native worker settles."""

    cleanup_complete: asyncio.Future[None]
    pump_hop_spans: _PumpHopSpans
    state: object


class _CancellationAwarePumpState(typ.Protocol):
    """State contract needed to determine a terminal span outcome."""

    was_cancelled: bool


def _complete_rust_pump(
    completed: asyncio.Future[int],
    *,
    completion: _RustPumpCompletion,
    logger: logging.Logger,
    restore_state: cabc.Callable[[object], None],
) -> None:
    """Close spans and restore asyncio state after the worker settles."""
    state = typ.cast("_CancellationAwarePumpState", completion.state)
    total_bytes: int | None = None
    outcome = "cancelled"
    try:
        if completed.cancelled():
            outcome = "cancelled"
        else:
            error = completed.exception()
            if error is not None:
                outcome = "failed_after_cancel" if state.was_cancelled else "failed"
                if state.was_cancelled:
                    _pump_obs._log_native_pump_failed_after_cancel(logger, error)
            elif state.was_cancelled:
                outcome = "cancelled"
            else:
                outcome = "succeeded"
                total_bytes = completed.result()
    finally:
        _close_pump_hop_spans(
            completion.pump_hop_spans,
            outcome=outcome,
            total_bytes=total_bytes,
        )
        try:
            with _suppressed_teardown_failure(
                logger,
                "restore_state",
                OSError,
                ValueError,
            ):
                restore_state(completion.state)
        finally:
            if not completion.cleanup_complete.done():
                completion.cleanup_complete.set_result(None)
