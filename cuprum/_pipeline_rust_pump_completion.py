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
from cuprum.pump_span_events import PumpHopOutcome

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import logging


class _CancellationAwarePumpState(typ.Protocol):
    """State contract needed to determine a terminal span outcome."""

    @property
    def was_cancelled(self) -> bool:
        """Whether cancellation reached the awaiting pump task."""
        raise NotImplementedError


class _CompletedPumpFuture(typ.Protocol):
    """Settled native-pump future used to classify a worker outcome."""

    def cancelled(self) -> bool:
        """Report whether the worker finished by cancellation."""

    def exception(self) -> BaseException | None:
        """Return the worker exception after it settles, if any."""

    def result(self) -> int:
        """Return the successful native-pump byte total."""


@dc.dataclass(frozen=True, slots=True)
class _RustPumpCompletionHooks:
    """Terminal callbacks owned by the native-pump lifecycle boundary."""

    close_spans: cabc.Callable[[PumpHopOutcome, int | None], None]
    restore_state: cabc.Callable[[], None]
    signal_completion: cabc.Callable[[], None]


def _classify_pump_outcome(
    completed: _CompletedPumpFuture,
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
    completed: _CompletedPumpFuture,
    *,
    state: StateT,
    hooks: _RustPumpCompletionHooks,
    logger: logging.Logger,
) -> None:
    """Run shared terminal handling through caller-owned cleanup hooks."""
    outcome: PumpHopOutcome = PumpHopOutcome.CANCELLED
    total_bytes: int | None = None
    try:
        outcome, total_bytes = _classify_pump_outcome(completed, state)
        if outcome is PumpHopOutcome.FAILED_AFTER_CANCEL:
            error = completed.exception()
            if error is not None:
                _pump_obs._log_native_pump_failed_after_cancel(logger, error)
    finally:
        try:
            hooks.close_spans(outcome, total_bytes)
        finally:
            try:
                hooks.restore_state()
            finally:
                hooks.signal_completion()
