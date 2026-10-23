"""Disposal of values returned by pump hooks, which must not break emission.

A pump hook is synchronous by contract, so a returned coroutine is closed. A
coroutine that was already started runs its ``finally`` block during that
close, and whatever the block raises must follow the channel's failure policy:
an ordinary exception is reported and absorbed, and a shutdown signal travels.
"""

from __future__ import annotations

import inspect
import logging

import pytest

from cuprum.pump_events import PumpEvent, RustPumpHandoffOutcome
from cuprum.pump_observation import _emit_rust_pump_handoff_outcome, observe_pump
from cuprum.unittests._hook_disposal_test_helpers import (
    _started_coroutine_raising,
    hook_returning,
)

_LOGGER_NAME = "cuprum.pump_observation"


def test_a_failing_disposal_is_reported_and_later_hooks_still_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception raised while closing a returned coroutine is absorbed."""
    received: list[PumpEvent] = []
    coro = _started_coroutine_raising(RuntimeError("close failed"))

    with (
        caplog.at_level(logging.WARNING, logger=_LOGGER_NAME),
        observe_pump(hook_returning(coro)),
        observe_pump(received.append),
    ):
        _emit_rust_pump_handoff_outcome(RustPumpHandoffOutcome.SUBMITTED)

    assert [event.outcome for event in received] == [
        RustPumpHandoffOutcome.SUBMITTED
    ], "a disposal failure must not skip later hooks"
    disposal = [
        record
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "pump_observer_disposal_failed"
    ]
    assert len(disposal) == 1, f"expected one disposal report, found {len(disposal)}"
    assert disposal[0].__dict__["cuprum_error_type"] == "RuntimeError", (
        "the disposal report must name the error raised during close"
    )
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED, (
        "the returned coroutine must be closed after its finally block fails"
    )


@pytest.mark.parametrize(
    "signal",
    [SystemExit(3), KeyboardInterrupt()],
    ids=["system-exit", "keyboard-interrupt"],
)
def test_a_shutdown_signal_raised_during_disposal_still_travels(
    signal: BaseException,
) -> None:
    """Disposal honours the same shutdown contract as the hook call itself."""
    coro = _started_coroutine_raising(signal)

    with observe_pump(hook_returning(coro)), pytest.raises(type(signal)):
        _emit_rust_pump_handoff_outcome(RustPumpHandoffOutcome.SUBMITTED)
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED, (
        "the returned coroutine must be closed before its fatal signal propagates"
    )
