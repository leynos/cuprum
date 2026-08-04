"""Registration and failure policy for the Rust-pump observation channel.

The failure policy here is a deliberate divergence from the exec-event channel,
which re-raises so a broken observe hook fails the command it was observing.
Both pump emission sites sit on paths whose contract is that they must complete
— the Python-pump fall-back and cancellation unwinding — so a raising collector
must not be able to turn a pipeline that would have succeeded into one that
fails. These tests pin that divergence in both directions: the failure is
reported, and it does not travel.
"""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import pytest

from cuprum import _pipeline_stream_fds
from cuprum.pump_events import PumpEvent, RustPumpDeclineReason
from cuprum.pump_observation import (
    _emit_pump_event,
    current_pump_hooks,
    observe_pump,
)
from cuprum.unittests._rust_pump_test_helpers import (
    decline_on_pause_failure,
    run_raw_fd_pump,
)

_LOGGER_NAME = "cuprum.pump_observation"
_DECLINE = PumpEvent(
    phase="declined",
    reason=RustPumpDeclineReason.RAW_FD_UNAVAILABLE,
)


class _BoomError(RuntimeError):
    """Stand-in for a metrics backend that fails while recording."""


def _raise_boom(_event: PumpEvent) -> None:
    """Fail the way a broken collector would."""
    raise _BoomError


def _observer_failure_records(
    caplog: pytest.LogCaptureFixture,
) -> list[logging.LogRecord]:
    """Return every recorded pump-observer failure."""
    return [
        record
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "pump_observer_failed"
    ]


def test_registration_is_scoped_to_its_block() -> None:
    """Hooks are visible inside the registration and gone after it."""
    seen: list[PumpEvent] = []

    assert current_pump_hooks() == (), "the channel must start empty"
    with observe_pump(seen.append):
        assert current_pump_hooks() == (seen.append,), (
            f"the hook must be registered inside its block, found "
            f"{current_pump_hooks()}"
        )
        _emit_pump_event(_DECLINE)
    assert current_pump_hooks() == (), "detaching must restore the prior hooks"

    _emit_pump_event(_DECLINE)
    assert len(seen) == 1, (
        f"only the event emitted inside the block may be delivered, got {seen}"
    )


def test_hooks_receive_events_in_registration_order() -> None:
    """Nested registrations deliver oldest-first, like the exec channel."""
    order: list[str] = []

    with (
        observe_pump(lambda _event: order.append("first")),
        observe_pump(lambda _event: order.append("second")),
    ):
        _emit_pump_event(_DECLINE)

    assert order == ["first", "second"], f"unexpected delivery order {order}"


def test_a_failing_hook_is_reported_and_the_rest_still_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One broken observer neither silences itself nor hides the next one."""
    seen: list[PumpEvent] = []

    with (
        caplog.at_level(logging.WARNING, logger=_LOGGER_NAME),
        observe_pump(_raise_boom),
        observe_pump(seen.append),
    ):
        _emit_pump_event(_DECLINE)

    records = _observer_failure_records(caplog)
    assert len(records) == 1, (
        f"a hook failure must be reported exactly once, found {len(records)}"
    )
    assert records[0].levelno == logging.WARNING, (
        "swallowing a hook failure quietly is what the policy forbids; it must "
        f"be a warning, found {records[0].levelname}"
    )
    assert records[0].exc_info is not None, (
        "the report must attach the traceback, or it names a failure without "
        "saying where it happened"
    )
    assert seen == [_DECLINE], (
        f"the surviving hook must still receive the event, got {seen}"
    )


def test_a_raising_collector_does_not_abort_the_fall_back(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken observer cannot break the hop it is observing.

    This is the requirement the divergence exists to satisfy. The decline site
    sits inside live stream pumping, and the hop it is about to fall back to
    would have succeeded; propagating here would abort a pipe hop because
    somebody's metrics backend was misconfigured.
    """
    with (
        caplog.at_level(logging.WARNING, logger=_LOGGER_NAME),
        observe_pump(_raise_boom),
    ):
        # No `pytest.raises`: reaching the assertion below *is* the assertion.
        decline_on_pause_failure(monkeypatch)

    assert _observer_failure_records(caplog), (
        "the collector's failure must still be reported, not discarded"
    )


def test_a_raising_collector_leaves_the_fall_back_signal_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hop still reports that it fell back, so the Python pump runs."""
    monkeypatch.setattr(
        _pipeline_stream_fds,
        "_pause_reader_transport",
        lambda _reader: _pipeline_stream_fds._ReaderPause(may_hand_off=False),
    )
    with observe_pump(_raise_boom):
        handled = run_raw_fd_pump()

    assert handled is False, (
        "a decline must still report the Python-fallback signal, or the hop "
        "silently transfers nothing"
    )


def test_a_shutdown_signal_from_a_hook_still_travels() -> None:
    """``SystemExit`` is not caught; a shutdown request must not be absorbed."""

    def exit_now(_event: PumpEvent) -> None:
        """Raise the way an interpreter shutdown would."""
        raise SystemExit(3)

    with observe_pump(exit_now), pytest.raises(SystemExit):
        _emit_pump_event(_DECLINE)


def test_a_cancellation_from_a_hook_still_travels() -> None:
    """``CancelledError`` propagates rather than being logged and dropped."""

    def cancel_now(_event: PumpEvent) -> None:
        """Raise the way a cancelled task would."""
        raise asyncio.CancelledError

    with observe_pump(cancel_now), pytest.raises(asyncio.CancelledError):
        _emit_pump_event(_DECLINE)


def test_an_awaitable_returning_hook_is_reported_and_closed(
    caplog: pytest.LogCaptureFixture,
    recwarn: pytest.WarningsRecorder,
) -> None:
    """A coroutine-returning hook is reported, and leaves no dangling warning.

    Pump hooks are synchronous by contract. An unclosed coroutine would
    resurface later as an unrelated ``never awaited`` warning attached to no
    useful context.
    """

    async def async_hook_body() -> None:
        """Do nothing; only its coroutine object matters here."""

    def async_hook(_event: PumpEvent) -> None:
        """Return a coroutine, which the contract does not allow."""
        return typ.cast("None", async_hook_body())

    with (
        caplog.at_level(logging.WARNING, logger=_LOGGER_NAME),
        observe_pump(async_hook),
    ):
        _emit_pump_event(_DECLINE)

    reported = [
        record
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "pump_observer_returned_value"
    ]
    assert len(reported) == 1, (
        f"an awaitable return must be reported once, found {len(reported)}"
    )
    never_awaited = [
        warning for warning in recwarn.list if "never awaited" in str(warning.message)
    ]
    assert not never_awaited, (
        f"the coroutine must be closed, but asyncio complained: {never_awaited}"
    )


def test_detaching_twice_is_harmless() -> None:
    """A second detach is a no-op rather than a context-var error."""
    registration = observe_pump(lambda _event: None)
    registration.detach()
    registration.detach()

    assert current_pump_hooks() == (), "the channel must be empty after detaching"
