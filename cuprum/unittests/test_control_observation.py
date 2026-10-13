"""Unit tests for the owned-handle control observation channel.

The channel exists so an operator can say what a control operation *did* to
its child, which the settled exit code cannot express. These tests pin the
properties that make it safe to sit on the path that signals a live child: an
unobserved run pays nothing, only a completed operation reports, and no
observer failure can escape the guard that invokes it.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import typing as typ
import warnings

import pytest

from cuprum.control_events import ControlOperation, ControlOutcome
from cuprum.control_observation import (
    _complete_control,
    _ControlTarget,
    _start_control,
    current_control_hooks,
    observe_control,
)
from cuprum.events import new_exec_id
from cuprum.unittests._adapter_test_support import capturing_logger
from cuprum.unittests._rust_pump_test_helpers import ControllableMonotonicClock

if typ.TYPE_CHECKING:
    from cuprum.control_events import ControlEvent, ControlHook

_LOGGER_NAME = "cuprum.control_observation"


def _collector() -> tuple[list[ControlEvent], ControlHook]:
    """Return a hook and the list it appends every received event to."""
    events: list[ControlEvent] = []
    return events, events.append


class TestRegistration:
    """Registering, scoping, and detaching a control observation hook."""

    def test_registers_a_hook_visible_to_the_current_context(self) -> None:
        """Register a hook and find it among the current context's hooks."""
        _events, hook = _collector()
        registration = observe_control(hook)

        assert hook in current_control_hooks(), "the hook is registered"

        registration.detach()

    def test_detaches_without_disturbing_other_hooks(self) -> None:
        """Detach one registration and leave every other hook in place."""
        _first_events, first = _collector()
        _second_events, second = _collector()
        first_registration = observe_control(first)
        second_registration = observe_control(second)

        first_registration.detach()

        remaining = current_control_hooks()
        assert first not in remaining, "the detached hook is gone"
        assert second in remaining, "an unrelated hook is untouched"

        second_registration.detach()

    def test_detaching_twice_removes_nothing_further(self) -> None:
        """Repeat a detach without letting it take a later hook with it."""
        _events, hook = _collector()
        registration = observe_control(hook)
        registration.detach()

        _later_events, later = _collector()
        later_registration = observe_control(later)
        registration.detach()

        assert current_control_hooks() == (later,), "only the detached hook was removed"

        later_registration.detach()

    def test_context_manager_detaches_on_exit(self) -> None:
        """Detach the registration when its ``with`` block ends."""
        _events, hook = _collector()

        with observe_control(hook) as registration:
            assert current_control_hooks() == (hook,), "the block observes"

        assert registration.__class__ is not None, "the block yields its handle"
        assert current_control_hooks() == (), "the hook is gone after the block"

    def test_registration_is_invisible_outside_its_context(self) -> None:
        """Confine a registration to the context that made it."""
        context = contextvars.copy_context()
        _events, hook = _collector()

        context.run(observe_control, hook)

        assert context.run(current_control_hooks) == (hook,), "its own context sees it"
        assert current_control_hooks() == (), "the surrounding context does not"


class TestMeasurement:
    """Starting, completing, and reporting one control operation."""

    def test_unobserved_operation_allocates_no_measurement(self) -> None:
        """Return no measurement, and read no clock, without a hook."""
        clock = ControllableMonotonicClock()

        assert _start_control(ControlOperation.SIGNAL, monotonic_clock=clock) is None

    def test_reports_the_measured_duration(self) -> None:
        """Measure the operation's span against the clock it was started with."""
        events, hook = _collector()
        clock = ControllableMonotonicClock(1.0)

        with observe_control(hook):
            measurement = _start_control(ControlOperation.KILL, monotonic_clock=clock)
            clock.value = 2.5
            _complete_control(measurement, ControlOutcome.DELIVERED)

        assert [event.duration_s for event in events] == [1.5], "the elapsed span"

    def test_reports_the_target_it_was_started_with(self) -> None:
        """Carry the run, the signal, and the child onto the completed event."""
        events, hook = _collector()
        exec_id = new_exec_id()

        with observe_control(hook):
            measurement = _start_control(
                ControlOperation.SIGNAL,
                target=_ControlTarget(exec_id=exec_id, signal_number=15, pid=4321),
            )
            _complete_control(measurement, ControlOutcome.DELIVERED)

        (event,) = events
        assert event.exec_id == exec_id, "the run's correlation token"
        assert event.signal_number == 15, "the caller-named signal"
        assert event.pid == 4321, "the child the operation was aimed at"

    def test_reports_no_target_when_none_was_given(self) -> None:
        """Default to an empty target rather than requiring one."""
        events, hook = _collector()

        with observe_control(hook):
            measurement = _start_control(ControlOperation.TERMINATE)
            _complete_control(measurement, ControlOutcome.ESCALATED)

        (event,) = events
        assert (event.exec_id, event.signal_number, event.pid) == (None, None, None), (
            "a producer with no target reports none"
        )

    def test_reports_nothing_until_the_operation_completes(self) -> None:
        """Emit nothing for an operation that has only started."""
        events, hook = _collector()

        with observe_control(hook):
            measurement = _start_control(ControlOperation.SIGNAL)
            assert measurement is not None, "an observed operation is measured"

        assert not events, "an incomplete operation reports nothing"

    def test_delivers_one_event_to_every_hook(self) -> None:
        """Send each completed operation to all registered hooks once."""
        first_events, first = _collector()
        second_events, second = _collector()

        with observe_control(first), observe_control(second):
            measurement = _start_control(ControlOperation.TERMINATE)
            _complete_control(measurement, ControlOutcome.DELIVERED)

        assert len(first_events) == 1, "the first hook received one event"
        assert first_events == second_events, "both hooks received the same event"

    def test_completing_without_a_measurement_reports_nothing(self) -> None:
        """Accept ``None`` so a caller can complete unconditionally."""
        events, hook = _collector()

        with observe_control(hook):
            _complete_control(None, ControlOutcome.DELIVERED)

        assert not events, "an unobserved operation emits nothing"


class TestFailOpen:
    """Keeping an observer's failure from reaching the control operation."""

    def test_a_raising_hook_is_reported_and_the_rest_continue(self) -> None:
        """Report the failure, then deliver to the hooks that follow."""
        events, recording = _collector()

        def failing(event: ControlEvent) -> None:
            """Refuse every event, the way a broken observer does."""
            msg = "observer is broken"
            raise RuntimeError(msg)

        with capturing_logger(_LOGGER_NAME) as captured:
            with observe_control(failing), observe_control(recording):
                measurement = _start_control(ControlOperation.SIGNAL)
                _complete_control(measurement, ControlOutcome.DELIVERED)
            records = captured.records

        assert len(events) == 1, "the later hook still received the event"
        actions = [
            record.__dict__.get("cuprum_action")
            for record in records
            if record.levelno == logging.WARNING
        ]
        assert actions == ["control_observer_failed"], "the failure is reported once"

    def test_a_hook_returning_a_value_is_reported(self) -> None:
        """Report a non-``None`` return rather than silently accepting it."""
        events, hook = _collector()

        def returns_a_value(event: ControlEvent) -> object:
            """Return a value, as an incorrectly declared hook would."""
            return object()

        with capturing_logger(_LOGGER_NAME) as captured:
            with observe_control(typ.cast("ControlHook", returns_a_value)):
                _complete_control(
                    _start_control(ControlOperation.SIGNAL),
                    ControlOutcome.DELIVERED,
                )
            records = captured.records

        assert not events, "the recording hook was never registered"
        actions = [record.__dict__.get("cuprum_action") for record in records]
        assert "control_observer_returned_value" in actions, "the return is reported"
        assert hook not in current_control_hooks(), "the odd hook stayed registered"

    def test_an_accidental_coroutine_is_closed(self) -> None:
        """Close a coroutine handed back by an incorrectly declared async hook."""
        coroutines: list[typ.Any] = []

        async def async_hook(event: ControlEvent) -> None:
            """Await nothing, as an async hook that was never awaited would."""

        def returns_a_coroutine(event: ControlEvent) -> object:
            """Return the coroutine object the guard has to close."""
            coroutine = async_hook(event)
            coroutines.append(coroutine)
            return coroutine

        with (
            warnings.catch_warnings(record=True) as raised,
            capturing_logger(_LOGGER_NAME),
            observe_control(typ.cast("ControlHook", returns_a_coroutine)),
        ):
            warnings.simplefilter("always")
            _complete_control(
                _start_control(ControlOperation.SIGNAL),
                ControlOutcome.DELIVERED,
            )
        for coroutine in coroutines:
            coroutine.close()
        collected = [
            warning for warning in raised if "never awaited" in str(warning.message)
        ]

        assert len(coroutines) == 1, "the hook produced one coroutine"
        assert coroutines[0].cr_frame is None, "the guard closed it"
        assert collected == [], "no never-awaited warning is raised later"

    @pytest.mark.parametrize(
        "raised",
        [asyncio.CancelledError, KeyboardInterrupt, SystemExit],
        ids=("cancellation", "interrupt", "exit"),
    )
    def test_an_observer_raising_above_exception_is_contained(
        self,
        raised: type[BaseException],
    ) -> None:
        """Contain a ``BaseException`` an observer raises, and report it.

        ``asyncio.CancelledError`` derives from ``BaseException``, so a guard
        catching only ``Exception`` lets it past — into the control path,
        where an escape abandons the settle or replaces a refusal the caller
        is owed. ``KeyboardInterrupt`` and ``SystemExit`` reach the same place
        by the same route: a hook is a bounded synchronous call inside the
        operation, not a place an interpreter-level exit belongs.

        The point of the case is the hook that comes *after* the failing one.
        Containment is only observable through it: if the exception escapes,
        the later hook never runs and the operation never completes.
        """
        events, recording = _collector()

        def refusing(event: ControlEvent) -> None:
            """Refuse every event, the way a broken observer does."""
            raise raised

        with capturing_logger(_LOGGER_NAME) as captured:
            with observe_control(refusing), observe_control(recording):
                measurement = _start_control(ControlOperation.SIGNAL)
                _complete_control(measurement, ControlOutcome.DELIVERED)
            records = captured.records

        assert len(events) == 1, "the later hook still received the event"
        assert events[0].operation is ControlOperation.SIGNAL, (
            "and received the operation that was being reported"
        )
        actions = [
            record.__dict__.get("cuprum_action")
            for record in records
            if record.levelno == logging.WARNING
        ]
        assert actions == ["control_observer_failed"], "the failure is reported"
        assert records[0].__dict__.get("cuprum_error_type") == raised.__name__, (
            "the report names the exception that was raised"
        )
