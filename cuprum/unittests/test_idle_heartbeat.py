"""Unit tests for the idle heartbeat's option contract and schedule.

``_IdleSchedule`` is pure state, so it is driven here from an explicit clock
with no real sleeping: the timing rules (reset on activity, one notification
per interval, no burst replay after a late wake) are pinned at exact instants
rather than approximated by a wall-clock assertion. Rendering a keepalive line,
reporting a failed one, and the watchdog that turns the schedule into real time
are pinned in ``test_idle_heartbeat_diagnostics.py``.
"""

from __future__ import annotations

import math
import typing as typ

import pytest

from cuprum._idle_heartbeat import _build_idle_monitor, _IdleSchedule
from cuprum.sh import IOOptions, RunOutputOptions

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_INTERVAL = 30.0
_PROGRAM = "cargo"


@pytest.mark.parametrize("interval", [0.0, -1.0, math.nan, math.inf, -math.inf])
def test_non_positive_or_non_finite_intervals_rejected(interval: float) -> None:
    """Example: idle intervals must be finite and strictly positive."""
    with pytest.raises(ValueError, match="idle_after"):
        RunOutputOptions(idle_after=interval)


@pytest.mark.parametrize("interval", ["soon", object()])
def test_non_numeric_intervals_rejected(interval: object) -> None:
    """Example: an interval that is not a number of seconds is a ``TypeError``."""
    with pytest.raises(TypeError, match="idle_after"):
        RunOutputOptions(idle_after=typ.cast("float", interval))


@pytest.mark.parametrize("interval", ["30", 30, "30.5", b"30"])
def test_coercible_intervals_are_normalized_at_construction(
    interval: object,
) -> None:
    """Example: an interval is stored as the float the schedule will use.

    ``_IdleSchedule.start`` evaluates ``now + interval``. Anything that merely
    *converts* to a float would raise ``TypeError`` from the watchdog's own
    task, after the child had already been spawned, so normalization has to
    happen here where the caller can still see it.
    """
    options = RunOutputOptions(idle_after=typ.cast("float", interval))
    assert isinstance(options.idle_after, float), (
        f"the stored interval must already be a float, got {options.idle_after!r}"
    )
    assert options.idle_after == pytest.approx(float(typ.cast("float", interval))), (
        "normalization must preserve the interval's value"
    )


def test_callback_requires_an_interval() -> None:
    """Example: ``on_idle`` without ``idle_after`` is rejected, not ignored."""
    with pytest.raises(ValueError, match="on_idle requires idle_after"):
        RunOutputOptions(on_idle=lambda _total, _idle: None)


def test_non_callable_callback_rejected() -> None:
    """Example: a non-callable ``on_idle`` is rejected at construction."""
    with pytest.raises(TypeError, match="on_idle must be callable"):
        RunOutputOptions(
            idle_after=_INTERVAL,
            on_idle=typ.cast("cabc.Callable[[float, float], None]", 3),
        )


def test_async_callbacks_rejected() -> None:
    """Example: an ``async def`` idle callback is rejected where detectable."""

    async def on_idle(_total: float, _idle: float) -> None:
        """Never awaited: the callback contract is synchronous."""

    with pytest.raises(TypeError, match="synchronous callback"):
        RunOutputOptions(
            idle_after=_INTERVAL,
            on_idle=typ.cast("cabc.Callable[[float, float], None]", on_idle),
        )


def test_async_callable_objects_rejected() -> None:
    """Example: an object whose ``__call__`` is async is rejected too."""

    class AsyncObserver:
        """Callable double whose invocation is a coroutine."""

        async def __call__(self, _total: float, _idle: float) -> None:
            """Never awaited: the contract is synchronous."""

    with pytest.raises(TypeError, match="synchronous callback"):
        RunOutputOptions(
            idle_after=_INTERVAL,
            on_idle=typ.cast("cabc.Callable[[float, float], None]", AsyncObserver()),
        )


def test_deprecated_alias_keeps_the_idle_contract() -> None:
    """Example: ``IOOptions`` still validates and still warns."""
    with pytest.warns(DeprecationWarning, match="IOOptions is deprecated"):
        options = IOOptions(idle_after=_INTERVAL)
    assert options.idle_after == _INTERVAL, "the idle interval must survive"
    with pytest.raises(ValueError, match="idle"):
        IOOptions(idle_after=0.0)


def test_defaults_disable_reporting_and_preserve_positional_order() -> None:
    """Example: the disabled default is free, and positional options still bind."""
    disabled = RunOutputOptions()
    assert (disabled.idle_after, disabled.on_idle) == (None, None), (
        "idle reporting must be off by default"
    )
    assert _build_idle_monitor(None, None, _PROGRAM) is None, (
        "a disabled heartbeat must not build a watchdog"
    )
    # Positional order is the contract under test, not an API flag a keyword
    # would express more clearly.
    positional = RunOutputOptions(False, True)  # ruff: ignore[boolean-positional-value-in-call] - positional order under test
    assert (positional.capture, positional.resolved_echo) == (False, (True, True)), (
        "capture and echo keep their positional order"
    )


def test_schedule_starts_one_interval_out() -> None:
    """Example: the first notification is due one interval after the start."""
    schedule = _IdleSchedule.start(_INTERVAL, now=100.0)
    assert schedule.seconds_until_due(100.0) == _INTERVAL, "first deadline mismatch"
    assert schedule.take_due(129.999) is None, (
        "no notification may precede its due time"
    )
    assert schedule.take_due(130.0) == (30.0, 30.0), "the due notification is missing"


def test_notification_measures_total_and_idle_apart() -> None:
    """Example: total counts from the start, idle from the last observed read."""
    schedule = _IdleSchedule.start(_INTERVAL, now=0.0)
    schedule.note_activity(60.0)
    assert schedule.take_due(90.0) == (90.0, 30.0), (
        "total must count from the start and idle from the last read"
    )


def test_notification_does_not_refresh_activity() -> None:
    """Example: idle time keeps growing across successive notifications."""
    schedule = _IdleSchedule.start(_INTERVAL, now=0.0)
    schedule.take_due(30.0)
    assert schedule.take_due(60.0) == (60.0, 60.0), (
        "an idle notification is not itself child activity"
    )


def test_late_wake_emits_once_and_reschedules_from_emission() -> None:
    """Example: a late loop emits a single notification, replaying no bursts."""
    schedule = _IdleSchedule.start(_INTERVAL, now=0.0)
    assert schedule.take_due(200.0) == (200.0, 200.0), "the late tick must fire once"
    assert schedule.seconds_until_due(200.0) == _INTERVAL, (
        "the next interval must run from the emission, not the missed deadline"
    )
    assert schedule.take_due(229.999) is None, "the catch-up tick must not replay"
    assert schedule.take_due(230.0) == (230.0, 230.0), "the next tick must still fire"


def test_activity_wins_over_a_stale_deadline() -> None:
    """Example: a read before the deadline cancels that notification entirely."""
    schedule = _IdleSchedule.start(_INTERVAL, now=0.0)
    schedule.note_activity(45.0)
    assert schedule.take_due(50.0) is None, "observed output must suppress the tick"
    assert schedule.take_due(75.0) == (75.0, 30.0), "the deadline must have moved"


def test_stopping_is_idempotent_and_terminal() -> None:
    """Example: a stopped run never notifies and ignores later activity."""
    schedule = _IdleSchedule.start(_INTERVAL, now=0.0)
    schedule.stop()
    schedule.stop()
    schedule.note_activity(10.0)
    assert schedule.take_due(1000.0) is None, "a stopped schedule must stay silent"
    assert schedule.last_activity == schedule.started_at, (
        "a stopped schedule must freeze its clock"
    )
