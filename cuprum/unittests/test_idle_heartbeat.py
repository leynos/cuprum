"""Unit tests for the idle heartbeat's option contract and state machine.

``_IdleSchedule`` is pure state and ``_IdleDiagnostic`` is pure rendering, so
both are driven here from an explicit clock with no real sleeping: the timing
rules (reset on activity, one notification per interval, no burst replay after
a late wake) are pinned at exact instants rather than approximated by a
wall-clock assertion. Only the watchdog driver itself needs an event loop, and
it is exercised with a short real interval and an ``asyncio.Event`` set by the
notification it is expected to produce.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import itertools
import math
import sys
import typing as typ

import pytest

from cuprum._idle_diagnostic import (
    _MAX_IDLE_LINE_BYTES,
    _format_duration,
    _idle_subject,
    _IdleDiagnostic,
    _LiveSink,
    _render_idle_line,
)
from cuprum._idle_heartbeat import (
    _build_idle_monitor,
    _IdleNotifier,
    _IdleSchedule,
    _stop_idle_monitor,
)
from cuprum.sh import IOOptions, RunOutputOptions
from cuprum.unittests._rust_pump_test_helpers import ControllableMonotonicClock

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_INTERVAL = 30.0
_PROGRAM = "cargo"
# Long enough that a scheduling hiccup cannot make the driver test flaky, short
# enough that a broken driver fails the test rather than the suite timeout.
_DRIVER_WAIT_S = 5.0


def _record(seen: list[tuple[float, float]]) -> cabc.Callable[[float, float], None]:
    """Return a callback appending each notification to *seen*."""

    def callback(elapsed_total: float, elapsed_idle: float) -> None:
        """Append one notification to the recorder."""
        seen.append((elapsed_total, elapsed_idle))

    return callback


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
        """Never awaited: the contract is synchronous."""

    with pytest.raises(TypeError, match="synchronous callback"):
        # The cast states the runtime-only contract under test: the annotation
        # cannot reject an async callback, so validation must.
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


@pytest.mark.parametrize(
    ("elapsed", "expected"),
    [
        (0.0, "0s"),
        (5.9, "5s"),
        (59.0, "59s"),
        (60.0, "1m00s"),
        (250.0, "4m10s"),
        (3600.0, "1h00m"),
        (7500.0, "2h05m"),
    ],
)
def test_durations_render_in_compact_units(elapsed: float, expected: str) -> None:
    """Example: durations stay short and unit-stable as they grow."""
    assert _format_duration(elapsed) == expected, (
        f"duration rendering mismatch for {elapsed!r}"
    )


def test_rendered_line_is_bounded_and_terminated() -> None:
    """Example: a pathological programme name cannot break the byte ceiling."""
    line = _render_idle_line(
        _idle_subject("program" * 100),
        elapsed_total=250.0,
        elapsed_idle=30.0,
    )
    assert len(line.encode("ascii")) <= _MAX_IDLE_LINE_BYTES, "line exceeds the bound"
    assert line.endswith(")\n"), "the line must end the parenthesis and the line"


def test_rendered_line_is_ascii_and_control_safe() -> None:
    """Example: injected control bytes and accents never reach the sink raw."""
    subject = _idle_subject("car\ngo\r\x1b[31mé")
    line = _render_idle_line(subject, elapsed_total=30.0, elapsed_idle=30.0)
    assert line.count("\n") == 1, "the line must occupy exactly one line"
    assert line.isascii(), "the line must survive an ASCII-only sink"
    for injected in ("\r", "\x1b", "car\n"):
        assert injected not in line, f"injected byte {injected!r} reached the line"


def test_sanitized_subject_covers_empty_and_unprintable_names() -> None:
    """Example: an unnamed or unprintable programme still renders a subject."""
    assert _idle_subject("") == "still running program", (
        "an empty programme name must fall back to a placeholder"
    )
    assert _idle_subject("\x00") == "still running ?", (
        "unprintable name characters are replaced, not dropped"
    )


def test_diagnostic_is_flushed_to_the_resolved_sink() -> None:
    """Example: the built-in heartbeat writes one flushed line to stderr."""
    sink = io.StringIO()
    diagnostic = _IdleDiagnostic(_idle_subject(_PROGRAM), destination=_LiveSink(sink))
    diagnostic.write(elapsed_total=250.0, elapsed_idle=30.0)
    written = sink.getvalue()
    assert written == "[cuprum] still running cargo (idle 30s, total 4m10s)\n", (
        f"unexpected keepalive line: {written!r}"
    )


def test_diagnostic_separates_an_unfinished_mirrored_line() -> None:
    """Example: the keepalive never becomes the tail of a mirrored line."""
    sink = io.StringIO()
    diagnostic = _IdleDiagnostic(
        _idle_subject(_PROGRAM),
        destination=_LiveSink(sink),
        is_line_open=lambda: True,
    )
    diagnostic.write(elapsed_total=30.0, elapsed_idle=30.0)
    assert sink.getvalue().startswith("\n[cuprum]"), "presentation separator missing"


def test_live_sink_resolves_lazily_to_stderr() -> None:
    """Example: the default destination is the live ``sys.stderr``."""
    assert _LiveSink()() is sys.stderr, "the unconfigured sink must be stderr"
    sink = io.StringIO()
    assert _LiveSink(sink)() is sink, "a configured sink must be honoured"


def test_callback_replaces_the_built_in_diagnostic() -> None:
    """Example: ``on_idle`` suppresses the renderer rather than joining it."""
    sink = io.StringIO()
    seen: list[tuple[float, float]] = []
    notifier = _IdleNotifier(
        callback=_record(seen),
        diagnostic=_IdleDiagnostic(
            _idle_subject(_PROGRAM), destination=_LiveSink(sink)
        ),
    )
    assert notifier(30.0, 30.0) is True, "a successful notification keeps the channel"
    assert seen == [(30.0, 30.0)], "the callback must receive the reported ages"
    assert sink.getvalue() == "", "the built-in renderer must not also run"


def test_callback_failure_disables_the_channel_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Example: one sanitized warning replaces an endlessly failing callback."""

    def on_idle(_total: float, _idle: float) -> None:
        """Fail the way an ordinary callback bug would."""
        msg = "callback exploded"
        raise RuntimeError(msg)

    notifier = _IdleNotifier(
        callback=on_idle,
        diagnostic=_IdleDiagnostic(_idle_subject(_PROGRAM), destination=_LiveSink()),
    )
    with caplog.at_level("WARNING", logger="cuprum.idle"):
        assert notifier(30.0, 30.0) is False, "a failure must disable the channel"
    assert [record.getMessage() for record in caplog.records] == [
        "idle_notification_disabled error=RuntimeError"
    ], f"expected one sanitized warning, got {caplog.records!r}"


def test_callback_control_flow_exceptions_propagate() -> None:
    """Example: ``KeyboardInterrupt`` is never converted into a warning."""

    def on_idle(_total: float, _idle: float) -> None:
        """Interrupt the run from inside the callback."""
        raise KeyboardInterrupt

    notifier = _IdleNotifier(
        callback=on_idle,
        diagnostic=_IdleDiagnostic(_idle_subject(_PROGRAM), destination=_LiveSink()),
    )
    with pytest.raises(KeyboardInterrupt):
        notifier(30.0, 30.0)


def test_accidental_coroutine_return_is_closed_and_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Example: a coroutine-returning callback is diagnosed, not left dangling."""

    async def on_idle(_total: float, _idle: float) -> None:
        """Return a coroutine that the caller never awaits."""

    returned = on_idle(0.0, 0.0)
    notifier = _IdleNotifier(
        callback=typ.cast(
            "cabc.Callable[[float, float], None]",
            lambda _total, _idle: returned,
        ),
        diagnostic=_IdleDiagnostic(_idle_subject(_PROGRAM), destination=_LiveSink()),
    )
    with caplog.at_level("WARNING", logger="cuprum.idle"):
        assert notifier(30.0, 30.0) is True, "a returned value is not a failure"
    assert inspect.getcoroutinestate(returned) == inspect.CORO_CLOSED, (
        "the returned coroutine must be closed"
    )
    assert [record.getMessage() for record in caplog.records] == [
        "idle_callback_returned_value result_type=coroutine"
    ], f"expected one report of the return value, got {caplog.records!r}"


def test_monitor_polls_only_once_armed_and_due() -> None:
    """Example: the watchdog arms at launch and fires exactly at the deadline."""

    async def exercise() -> list[tuple[float, float]]:
        """Drive one monitor from an explicit clock and settle its driver."""
        clock = ControllableMonotonicClock()
        seen: list[tuple[float, float]] = []
        monitor = _build_idle_monitor(_INTERVAL, _record(seen), _idle_subject(_PROGRAM))
        assert monitor is not None, "an interval must build a monitor"
        # The clock is replaced before arming so no real second ever elapses;
        # only ``launch`` reads it to fix the run's zero point.
        clock.advance(5.0)
        monitor.clock = clock
        assert monitor.is_running is False, "a built monitor must not be armed yet"
        monitor.poll()
        assert seen == [], "an unlaunched monitor must not notify"
        monitor.launch()
        assert monitor.is_running is True, "launch must arm the heartbeat"
        # Halves are exact in binary floating point, so the poll just before the
        # deadline and the poll on it straddle the boundary by construction.
        clock.advance(_INTERVAL - 0.5)
        monitor.poll()
        assert seen == [], "a poll before the deadline must stay silent"
        clock.advance(0.5)
        monitor.poll()
        await _stop_idle_monitor(monitor)
        return seen

    assert asyncio.run(exercise()) == [(_INTERVAL, _INTERVAL)], (
        "the due poll must notify exactly once"
    )


def test_monitor_forwards_activity_and_stops_after_a_failure() -> None:
    """Example: activity defers the deadline, and a failure stops the channel."""

    async def exercise() -> tuple[list[tuple[float, float]], bool]:
        """Drive one failing monitor through an activity reset and a failure."""
        clock = ControllableMonotonicClock()
        seen: list[tuple[float, float]] = []

        def on_idle(total: float, idle: float) -> None:
            """Record the notification, then fail the channel."""
            seen.append((total, idle))
            msg = "callback exploded"
            raise RuntimeError(msg)

        monitor = _build_idle_monitor(_INTERVAL, on_idle, _idle_subject(_PROGRAM))
        assert monitor is not None, "an interval must build a monitor"
        monitor.clock = clock
        monitor.launch()
        clock.advance(29.0)
        monitor.note_activity()
        clock.advance(_INTERVAL)
        monitor.poll()
        assert monitor.is_running is False, (
            "a failed notification must stop the channel"
        )
        clock.advance(_INTERVAL)
        monitor.poll()
        await _stop_idle_monitor(monitor)
        return seen, monitor.is_running

    seen, is_running = asyncio.run(exercise())
    assert seen == [(59.0, 30.0)], "activity must reset the reported idle age"
    assert is_running is False, "a failed notification must stay stopped"


def test_driver_notifies_then_stops_without_leaving_a_task() -> None:
    """Example: the watchdog task fires once and settles on stop."""

    async def exercise() -> None:
        """Run a short-interval driver to its first notification, then stop it."""
        fired = asyncio.Event()

        def on_idle(_total: float, _idle: float) -> None:
            """Release the test once the driver has notified."""
            fired.set()

        monitor = _build_idle_monitor(0.01, on_idle, _idle_subject(_PROGRAM))
        assert monitor is not None, "an interval must build a monitor"
        monitor.launch()
        await asyncio.wait_for(fired.wait(), _DRIVER_WAIT_S)
        await _stop_idle_monitor(monitor)
        assert monitor.task is None, "the settled driver handle must be cleared"
        assert monitor.is_running is False, "a stopped monitor must not report running"
        await _stop_idle_monitor(monitor)

    asyncio.run(exercise())


def test_stopping_an_unlaunched_monitor_is_a_no_op() -> None:
    """Example: teardown may stop a heartbeat that never armed."""
    monitor = _build_idle_monitor(_INTERVAL, None, _idle_subject(_PROGRAM))
    assert monitor is not None, "an interval must build a monitor"
    asyncio.run(_stop_idle_monitor(monitor))
    assert monitor.is_running is False, "an unlaunched monitor must be stopped"
    asyncio.run(_stop_idle_monitor(None))


@pytest.mark.parametrize(
    ("capture", "echo"), list(itertools.product([False, True], repeat=2))
)
def test_capture_and_echo_combinations_still_build_one_monitor(
    *,
    capture: bool,
    echo: bool,
) -> None:
    """Example: idle reporting is independent of the capture/echo matrix."""
    options = RunOutputOptions(capture=capture, echo=echo, idle_after=_INTERVAL)
    monitor = _build_idle_monitor(
        options.idle_after,
        options.on_idle,
        _idle_subject(_PROGRAM),
    )
    assert monitor is not None, f"capture={capture}, echo={echo} lost its monitor"
    assert monitor.notify.callback is None, (
        "the built-in diagnostic must be the default"
    )
