"""Registration and failure policy for capture outcome hooks."""

from __future__ import annotations

import asyncio
import inspect
import logging
import typing as typ

import pytest

from cuprum.capture_events import CaptureEvent, CaptureEventCategory
from cuprum.capture_observation import (
    _emit_capture_event,
    current_capture_hooks,
    observe_capture,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.capture_events import CaptureHook


_LOGGER_NAME = "cuprum.capture_observation"
_TRUNCATED = CaptureEvent(
    stream="stdout",
    category=CaptureEventCategory.TRUNCATED,
    total_bytes=8,
    dropped_bytes=3,
)


class _ObserverError(RuntimeError):
    """Stand-in for a metrics backend that fails while recording."""


def _raise_observer_error(_event: CaptureEvent) -> None:
    """Fail the way a broken collector would."""
    raise _ObserverError


def _observer_failures(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return capture-observer failure records from the log fixture."""
    return [
        record
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "capture_observer_failed"
    ]


def test_registration_is_scoped_and_restores_the_prior_hooks() -> None:
    """A registered hook sees events only inside its context."""
    seen: list[CaptureEvent] = []
    hook = seen.append

    assert current_capture_hooks() == (), "the channel must start empty"
    with observe_capture(hook):
        assert current_capture_hooks() == (hook,), "the hook must be registered"
        _emit_capture_event(_TRUNCATED)

    assert current_capture_hooks() == (), "scope exit must restore the empty tuple"
    _emit_capture_event(_TRUNCATED)
    assert seen == [_TRUNCATED], f"only the scoped event may be delivered: {seen}"


def test_nested_registrations_keep_order_and_restore_the_outer_hook() -> None:
    """Nested hooks run oldest first and detach in last-in-first-out order."""
    order: list[str] = []

    def first(_event: CaptureEvent) -> None:
        """Record the first registration's position."""
        order.append("first")

    def second(_event: CaptureEvent) -> None:
        """Record the second registration's position."""
        order.append("second")

    with observe_capture(first):
        with observe_capture(second):
            assert current_capture_hooks() == (first, second), (
                "the inner scope must retain both registrations"
            )
            _emit_capture_event(_TRUNCATED)
        assert current_capture_hooks() == (first,), (
            "leaving the inner scope must restore only the outer hook"
        )

    assert order == ["first", "second"], f"unexpected delivery order: {order}"
    assert current_capture_hooks() == (), "both scopes must restore the empty tuple"


def test_detaching_twice_is_harmless() -> None:
    """A repeated detach does not leave a hook or fail on its old token."""
    seen: list[CaptureEvent] = []
    registration = observe_capture(seen.append)

    registration.detach()
    registration.detach()
    _emit_capture_event(_TRUNCATED)

    assert current_capture_hooks() == (), "the hook must be gone after detaching"
    assert not seen, f"a detached hook must receive no events: {seen}"


async def _emit_under_own_hook(
    hook: CaptureHook,
    event: CaptureEvent,
    barrier: asyncio.Barrier,
) -> tuple[CaptureHook, ...]:
    """Emit one event while a sibling task has its own registration."""
    with observe_capture(hook):
        await barrier.wait()
        visible = current_capture_hooks()
        _emit_capture_event(event)
        await barrier.wait()
    return visible


async def _register_in_sibling_tasks() -> tuple[
    list[CaptureEvent],
    list[CaptureEvent],
    tuple[CaptureHook, ...],
    tuple[CaptureHook, ...],
]:
    """Return events and registrations observed by two concurrent tasks."""
    seen_a: list[CaptureEvent] = []
    seen_b: list[CaptureEvent] = []
    event_a = _TRUNCATED
    event_b = CaptureEvent(
        stream="stderr",
        category=CaptureEventCategory.SPILLED_TO_FILE,
        spilled_bytes=8,
    )
    barrier = asyncio.Barrier(2)

    visible_a, visible_b = await asyncio.gather(
        _emit_under_own_hook(seen_a.append, event_a, barrier),
        _emit_under_own_hook(seen_b.append, event_b, barrier),
    )
    return seen_a, seen_b, visible_a, visible_b


def test_registrations_in_sibling_tasks_stay_isolated() -> None:
    """Concurrent tasks see and deliver only their own capture hooks."""
    seen_a, seen_b, visible_a, visible_b = asyncio.run(_register_in_sibling_tasks())

    assert seen_a == [_TRUNCATED], f"task A received the wrong events: {seen_a}"
    assert len(seen_b) == 1, f"task B must receive one event: {seen_b}"
    assert seen_b[0].stream == "stderr", f"task B received task A's event: {seen_b}"
    assert len(visible_a) == len(visible_b) == 1, (
        f"each task must see only its own hook: {visible_a}, {visible_b}"
    )
    assert set(visible_a).isdisjoint(visible_b), (
        f"task registrations must be disjoint: {visible_a}, {visible_b}"
    )
    assert current_capture_hooks() == (), "task hooks must not survive in the parent"


def test_a_failing_hook_is_logged_and_remaining_hooks_still_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One broken observer is reported without silencing later hooks."""
    seen: list[CaptureEvent] = []

    with (
        caplog.at_level(logging.WARNING, logger=_LOGGER_NAME),
        observe_capture(_raise_observer_error),
        observe_capture(seen.append),
    ):
        _emit_capture_event(_TRUNCATED)

    failures = _observer_failures(caplog)
    assert len(failures) == 1, f"expected one observer warning, got {failures}"
    assert failures[0].levelno == logging.WARNING, "observer failure must be logged"
    assert failures[0].exc_info is not None, "observer warning must include a traceback"
    assert failures[0].__dict__.get("cuprum_error_type") == "_ObserverError", (
        "the log's structured error field must contain only the exception class"
    )
    assert seen == [_TRUNCATED], f"the next registered hook must still run: {seen}"


def test_a_non_none_hook_return_is_logged_and_its_coroutine_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A synchronous hook returning an accidental coroutine is reported."""
    created: list[cabc.Coroutine[object, object, None]] = []

    async def async_hook_body() -> None:
        """Provide a coroutine object for the synchronous hook to return."""
        await asyncio.sleep(0)

    def async_hook(_event: CaptureEvent) -> None:
        """Return a coroutine, which the synchronous hook contract forbids."""
        coroutine = async_hook_body()
        created.append(coroutine)
        return typ.cast("None", coroutine)

    with (
        caplog.at_level(logging.WARNING, logger=_LOGGER_NAME),
        observe_capture(async_hook),
    ):
        _emit_capture_event(_TRUNCATED)

    returned_values = [
        record
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "capture_observer_returned_value"
    ]
    assert len(returned_values) == 1, (
        f"the accidental result must be reported once: {returned_values}"
    )
    assert returned_values[0].__dict__.get("cuprum_result_type") == "coroutine", (
        f"the warning must name only the returned type: {returned_values[0]}"
    )
    assert len(created) == 1, f"the hook must run once, found {len(created)} coroutines"
    assert inspect.getcoroutinestate(created[0]) == inspect.CORO_CLOSED, (
        "the returned coroutine must be closed to prevent a detached warning"
    )


def test_a_base_exception_from_a_hook_still_propagates() -> None:
    """Shutdown signals are not swallowed by the observer failure policy."""
    cancellation = asyncio.CancelledError("stop capture delivery")

    def cancel(_event: CaptureEvent) -> None:
        """Raise the cancellation signal while observing an event."""
        raise cancellation

    with observe_capture(cancel), pytest.raises(asyncio.CancelledError) as caught:
        _emit_capture_event(_TRUNCATED)

    assert caught.value is cancellation, "the original cancellation must propagate"
