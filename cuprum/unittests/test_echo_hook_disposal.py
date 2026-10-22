"""Echo-hook disposal reports ordinary failures and propagates fatal signals."""

from __future__ import annotations

import asyncio
import inspect
import logging

import pytest

from cuprum.echo_events import EchoErrorCategory, EchoEvent, EchoStream
from cuprum.echo_observation import _emit_echo_event, observe_echo
from cuprum.unittests._hook_disposal_test_helpers import (
    _started_coroutine_raising,
    hook_returning,
)

_LOGGER_NAME = "cuprum.echo_observation"


@pytest.mark.parametrize(
    "signal",
    [SystemExit(3), KeyboardInterrupt(), asyncio.CancelledError()],
    ids=["system-exit", "keyboard-interrupt", "cancelled-error"],
)
def test_a_shutdown_signal_raised_during_disposal_still_travels(
    signal: BaseException,
) -> None:
    """Echo disposal preserves shutdown and cancellation signals."""
    coro = _started_coroutine_raising(signal)
    event = EchoEvent(
        stream=EchoStream.STDOUT,
        error_category=EchoErrorCategory.UNICODE_ENCODE,
    )

    with (
        observe_echo(hook_returning(coro)),
        pytest.raises(type(signal)),
    ):
        _emit_echo_event(event)

    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED, (
        "the returned coroutine must be closed before its fatal signal propagates"
    )


def test_a_failing_disposal_is_reported_and_later_hooks_still_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception raised while closing a returned coroutine is absorbed."""
    event = EchoEvent(
        stream=EchoStream.STDOUT,
        error_category=EchoErrorCategory.UNICODE_ENCODE,
    )
    received: list[EchoEvent] = []
    coro = _started_coroutine_raising(RuntimeError("close failed"))

    with (
        caplog.at_level(logging.WARNING, logger=_LOGGER_NAME),
        observe_echo(hook_returning(coro)),
        observe_echo(received.append),
    ):
        _emit_echo_event(event)

    assert received == [event], "a disposal failure must not skip later hooks"
    disposal = [
        record
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "echo_observer_disposal_failed"
    ]
    assert len(disposal) == 1, f"expected one disposal report, found {len(disposal)}"
    assert disposal[0].levelno == logging.WARNING, (
        "disposal failures must be reported at WARNING"
    )
    assert disposal[0].__dict__["cuprum_error_type"] == "RuntimeError", (
        "the disposal report must name the error raised during close"
    )
    assert disposal[0].__dict__["cuprum_stream"] == str(event.stream), (
        "the disposal report must identify the echo stream"
    )
    assert disposal[0].exc_info is not None, (
        "the disposal report must include the closing traceback"
    )
    returned = [
        record
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "echo_observer_returned_value"
    ]
    assert len(returned) == 1, (
        f"expected one returned-value warning, found {len(returned)}"
    )
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED, (
        "the returned coroutine must be closed after its finally block fails"
    )
