"""Unit contracts for Rust-pump completion classification and cleanup."""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import pytest

from cuprum._pipeline_rust_pump_completion import (
    _classify_pump_outcome,
    _complete_rust_pump,
    _RustPumpCompletion,
)
from cuprum.pump_span_events import PumpHopOutcome
from cuprum.pump_span_observation import _PumpHopSpans

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _CompletionState:
    """Minimal state for completion outcome and restoration tests."""

    def __init__(self, *, was_cancelled: bool) -> None:
        """Record whether cancellation reached the awaiting pump task."""
        self.was_cancelled = was_cancelled


class _InterruptingSpan:
    """Span double that aborts terminal attribute recording."""

    def __init__(self) -> None:
        """Start without an end notification."""
        self.ended = False

    def set_attribute(self, key: str, value: object) -> None:
        """Propagate the control-flow interruption under test."""
        raise KeyboardInterrupt

    def set_status(self, *, ok: bool) -> None:
        """Implement the span protocol; this test never reaches it."""
        del ok

    def add_event(
        self,
        name: str,
        attributes: cabc.Mapping[str, object] | None = None,
    ) -> None:
        """Implement the span protocol; this test never reaches it."""

    def end(self) -> None:
        """Record that the span closer still invoked finalization."""
        self.ended = True


def test_classify_pump_outcome_covers_every_terminal_state() -> None:
    """Completion classification keeps all four bounded outcome values explicit."""
    asyncio.run(_assert_all_pump_outcomes())


async def _assert_all_pump_outcomes() -> None:
    """Build each future terminal state in the loop that owns it."""
    await asyncio.sleep(0)
    loop = asyncio.get_running_loop()

    succeeded = loop.create_future()
    succeeded.set_result(29)
    assert _classify_pump_outcome(succeeded, _CompletionState(was_cancelled=False)) == (
        PumpHopOutcome.SUCCEEDED,
        29,
    )

    failed = loop.create_future()
    failed.set_exception(OSError("worker failed"))
    assert _classify_pump_outcome(failed, _CompletionState(was_cancelled=False)) == (
        PumpHopOutcome.FAILED,
        None,
    )

    cancelled = loop.create_future()
    cancelled.cancel()
    assert _classify_pump_outcome(cancelled, _CompletionState(was_cancelled=False)) == (
        PumpHopOutcome.CANCELLED,
        None,
    )

    failed_after_cancel = loop.create_future()
    failed_after_cancel.set_exception(OSError("worker failed after cancellation"))
    assert _classify_pump_outcome(
        failed_after_cancel,
        _CompletionState(was_cancelled=True),
    ) == (PumpHopOutcome.FAILED_AFTER_CANCEL, None)


def test_completion_restores_state_when_span_closure_aborts() -> None:
    """Observer control flow cannot strand the native-pump cleanup waiter."""
    asyncio.run(_assert_cleanup_after_span_control_flow())


async def _assert_cleanup_after_span_control_flow() -> None:
    """Run completion with an interrupting span and inspect unconditional cleanup."""
    await asyncio.sleep(0)
    loop = asyncio.get_running_loop()
    completed = loop.create_future()
    completed.set_result(0)
    cleanup_complete = loop.create_future()
    state = _CompletionState(was_cancelled=False)
    restored: list[_CompletionState] = []
    span = _InterruptingSpan()
    completion = _RustPumpCompletion(
        cleanup_complete=cleanup_complete,
        pump_hop_spans=_PumpHopSpans((span,)),
        state=state,
        restore_state=restored.append,
    )

    with pytest.raises(KeyboardInterrupt):
        _complete_rust_pump(
            completed,
            completion=completion,
            logger=logging.getLogger(__name__),
        )

    assert span.ended is True, "span finalization must still run before propagating"
    assert restored == [state], "descriptor state must restore after span failure"
    assert cleanup_complete.done(), "cancellation cleanup waiter must always settle"
