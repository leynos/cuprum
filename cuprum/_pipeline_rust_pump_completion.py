"""Settle Rust-pump executor futures without leaking stream resources.

This module owns executor completion callbacks: it classifies a native worker's
terminal outcome, closes its executor-hop spans after worker settlement,
restores the asyncio stream state, and signals the cancellation cleanup waiter.
Those cleanup responsibilities run even when an observer aborts span closure.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum import _pipeline_stream_cleanup_observation as _pump_obs
from cuprum._pipeline_stream_fds import _suppressed_teardown_failure
from cuprum.pump_span_events import PumpHopOutcome
from cuprum.pump_span_observation import _close_pump_hop_spans

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc
    import logging

    from cuprum.pump_span_observation import _PumpHopSpans


class _CancellationAwarePumpState(typ.Protocol):
    """State contract needed to determine a terminal span outcome."""

    was_cancelled: bool


@dc.dataclass(frozen=True, slots=True)
class _RustPumpCompletion[StateT: _CancellationAwarePumpState]:
    """Resources whose lifetime ends when the native worker settles."""

    cleanup_complete: asyncio.Future[None]
    pump_hop_spans: _PumpHopSpans
    state: StateT
    restore_state: cabc.Callable[[StateT], None]


def _classify_pump_outcome(
    completed: asyncio.Future[int],
    state: _CancellationAwarePumpState,
) -> tuple[PumpHopOutcome, int | None]:
    """Return the worker's bounded outcome and transferred-byte total."""
    if completed.cancelled():
        return PumpHopOutcome.CANCELLED, None
    if completed.exception() is not None:
        outcome = (
            PumpHopOutcome.FAILED_AFTER_CANCEL
            if state.was_cancelled
            else PumpHopOutcome.FAILED
        )
        return outcome, None
    if state.was_cancelled:
        return PumpHopOutcome.CANCELLED, None
    return PumpHopOutcome.SUCCEEDED, completed.result()


def _complete_rust_pump[StateT: _CancellationAwarePumpState](
    completed: asyncio.Future[int],
    *,
    completion: _RustPumpCompletion[StateT],
    logger: logging.Logger,
) -> None:
    """Close spans and restore asyncio state after the worker settles."""
    outcome: PumpHopOutcome = PumpHopOutcome.CANCELLED
    total_bytes: int | None = None
    try:
        outcome, total_bytes = _classify_pump_outcome(completed, completion.state)
        if outcome is PumpHopOutcome.FAILED_AFTER_CANCEL:
            error = completed.exception()
            if error is not None:
                _pump_obs._log_native_pump_failed_after_cancel(logger, error)
    finally:
        try:
            _close_pump_hop_spans(
                completion.pump_hop_spans,
                outcome=outcome,
                total_bytes=total_bytes,
            )
        finally:
            try:
                with _suppressed_teardown_failure(
                    logger,
                    "restore_state",
                    OSError,
                    ValueError,
                ):
                    completion.restore_state(completion.state)
            finally:
                if not completion.cleanup_complete.done():
                    completion.cleanup_complete.set_result(None)
