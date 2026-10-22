"""Stream-operation hook disposal reports failures and propagates fatal signals."""

from __future__ import annotations

import asyncio
import inspect
import logging

import pytest

from cuprum.stream_events import (
    StreamOperation,
    StreamOperationEvent,
    StreamOperationOutcome,
)
from cuprum.stream_observation import _emit_stream_operation_event
from cuprum.unittests._hook_disposal_test_helpers import (
    _started_coroutine_raising,
    hook_returning,
)

_LOGGER_NAME = "cuprum.stream_observation"


def _event() -> StreamOperationEvent:
    """Build the minimal completed drain event used by disposal cases."""
    return StreamOperationEvent(
        operation=StreamOperation.DRAIN,
        outcome=StreamOperationOutcome.EOF,
        bytes_consumed=0,
        read_operations=0,
        duration_s=0.0,
    )


@pytest.mark.parametrize(
    "signal",
    [SystemExit(3), KeyboardInterrupt(), asyncio.CancelledError()],
    ids=["system-exit", "keyboard-interrupt", "cancelled-error"],
)
def test_a_shutdown_signal_raised_during_disposal_still_travels(
    signal: BaseException,
) -> None:
    """Stream-operation disposal preserves shutdown and cancellation signals."""
    coro = _started_coroutine_raising(signal)

    with pytest.raises(type(signal)):
        _emit_stream_operation_event(_event(), hooks=(hook_returning(coro),))

    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED, (
        "the returned coroutine must be closed before its fatal signal propagates"
    )


def test_a_failing_disposal_is_reported_and_later_hooks_still_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception raised while closing a returned coroutine is absorbed."""
    event = _event()
    received: list[StreamOperationEvent] = []
    coro = _started_coroutine_raising(RuntimeError("close failed"))

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        _emit_stream_operation_event(
            event,
            hooks=(hook_returning(coro), received.append),
        )

    assert received == [event], "a disposal failure must not skip later hooks"
    disposal = [
        record
        for record in caplog.records
        if record.__dict__.get("cuprum_action")
        == "stream_operation_observer_disposal_failed"
    ]
    assert len(disposal) == 1, f"expected one disposal report, found {len(disposal)}"
    assert disposal[0].levelno == logging.WARNING, (
        "disposal failures must be reported at WARNING"
    )
    assert disposal[0].__dict__["cuprum_error_type"] == "RuntimeError", (
        "the disposal report must name the error raised during close"
    )
    assert disposal[0].__dict__["cuprum_operation"] == str(event.operation), (
        "the disposal report must identify the stream operation"
    )
    assert "cuprum_outcome" not in disposal[0].__dict__, (
        "the disposal report must not include the event outcome"
    )
    assert disposal[0].exc_info is not None, (
        "the disposal report must include the closing traceback"
    )
    returned = [
        record
        for record in caplog.records
        if record.__dict__.get("cuprum_action")
        == "stream_operation_observer_returned_value"
    ]
    assert len(returned) == 1, (
        f"expected one returned-value warning, found {len(returned)}"
    )
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED, (
        "the returned coroutine must be closed after its finally block fails"
    )
