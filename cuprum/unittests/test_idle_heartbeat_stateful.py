"""Hypothesis state machine over random idle-heartbeat interleavings.

A random sequence of clock advances, child output, notification attempts,
callback failures, and stops is driven through the watchdog and cross-checked
against a model. The model keeps the schedule's own numbers -- start, last
activity, next deadline, stopped -- but moves them by the rule that was
applied, so an implementation that moved a deadline for the wrong reason
disagrees with it instead of silently agreeing with itself. The built-in
renderer's lines are counted at the sink, which makes the channel's failure
mode observable as model state rather than as a log line.

The pinned boundary cases live in ``test_idle_heartbeat.py``; this module
explores the orderings no handwritten case would think to cover. The driver
task that turns the schedule into real time is deliberately left out -- it
needs a live event loop -- and is covered by the example and integration tests.
"""

from __future__ import annotations

import io
import typing as typ

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from cuprum._idle_diagnostic import _idle_subject
from cuprum._idle_heartbeat import _build_idle_monitor, _IdleMonitor, _IdleSchedule
from cuprum.unittests._rust_pump_test_helpers import ControllableMonotonicClock

_INTERVAL = st.floats(
    min_value=0.5, max_value=60.0, allow_nan=False, allow_infinity=False
)
_CLOCK_STEP = st.floats(
    min_value=0.0, max_value=900.0, allow_nan=False, allow_infinity=False
)
_PROGRAM = "cargo"


def _fail(_elapsed_total: float, _elapsed_idle: float) -> None:
    """Fail a notification the way an ordinary callback bug would."""
    msg = "callback exploded"
    raise RuntimeError(msg)


class _IdleHeartbeatMachine(RuleBasedStateMachine):
    """Drive random heartbeat interleavings through a model of the schedule."""

    def __init__(self) -> None:
        """Start with an idle run that must be created by ``setup``."""
        super().__init__()
        self._monitor: _IdleMonitor | None = None
        self._sink = io.StringIO()
        self._fail_next = False
        self._interval = 0.0
        self._started_at = 0.0
        self._last_activity = 0.0
        self._next_deadline = 0.0
        self._is_stopped = False
        self._now = 0.0
        self._delivered = 0

    def _begin(self, interval: float, now: float) -> None:
        """Reset the model and the monitor to a freshly armed run."""
        # A later run gets its own destination along with its own watchdog, so
        # the lines an earlier run left behind are not counted against it.
        self._sink = io.StringIO()
        monitor = _build_idle_monitor(
            interval,
            None,
            _idle_subject(_PROGRAM),
            self._sink,
        )
        assert monitor is not None, "an interval must build a monitor"
        clock = ControllableMonotonicClock(now)
        monitor.clock = clock
        # Armed exactly as ``launch`` arms it, minus the driver task: the rules
        # below are the driver's own steps, taken on demand.
        monitor.schedule = _IdleSchedule.start(interval, clock())
        self._interval = interval
        self._started_at = now
        self._last_activity = now
        self._next_deadline = now + interval
        self._is_stopped = False
        self._now = now
        self._delivered = 0
        self._rendered = 0
        # A new run gets a built-in renderer, whatever the last one was armed
        # with; the model has to forget the old arming with it.
        self._fail_next = False
        self._monitor = monitor

    def _armed(self) -> _IdleMonitor:
        """Return the monitor under test, which ``setup`` must have created."""
        monitor = self._monitor
        assert monitor is not None, "setup must build a monitor first"
        return monitor

    def _schedule(self) -> _IdleSchedule:
        """Return the armed schedule, which ``setup`` must have created."""
        schedule = self._armed().schedule
        assert schedule is not None, "setup must arm a schedule"
        return schedule

    def _lines_written(self) -> int:
        """Count the keepalive lines the built-in renderer produced."""
        return len(self._sink.getvalue().splitlines())

    @initialize(interval=_INTERVAL, now=_CLOCK_STEP)
    def setup(self, interval: float, now: float) -> None:
        """Arm one run whose clock starts at ``now``."""
        self._begin(interval, now)

    @precondition(lambda self: self._is_stopped)
    @rule(interval=_INTERVAL, now=_CLOCK_STEP)
    def relaunch(self, interval: float, now: float) -> None:
        """Give a later run its own watchdog once the previous one stopped.

        Restarting only after a stop keeps one monitor's whole lifetime inside
        a single example, which is what makes the terminal-state invariants
        meaningful.
        """
        self._begin(interval, now)

    @rule(seconds=_CLOCK_STEP)
    def advance(self, seconds: float) -> None:
        """Move the run's clock forward."""
        typ.cast("ControllableMonotonicClock", self._armed().clock).advance(seconds)
        self._now += seconds

    @rule()
    def child_output(self) -> None:
        """Report a non-empty read on a monitored stream."""
        self._armed().note_activity()
        if not self._is_stopped:
            self._last_activity = self._now
            self._next_deadline = self._now + self._interval

    @rule()
    def arm_a_failing_callback(self) -> None:
        """Make the next notification fail the way an ordinary bug would."""
        self._fail_next = True
        self._armed().notify.callback = _fail

    @rule()
    def arm_the_built_in_renderer(self) -> None:
        """Make the next notification a rendered keepalive line."""
        self._fail_next = False
        self._armed().notify.callback = None

    @rule()
    def poll(self) -> None:
        """Attempt one notification and check what the schedule reported."""
        stopped_before = self._is_stopped
        was_due = self._now >= self._next_deadline
        delivered_before = self._delivered
        self._armed().poll()
        if stopped_before or not was_due:
            assert self._delivered == delivered_before, (
                "a poll that cannot be due must not notify for "
                f"stopped={stopped_before}, due={was_due}, now={self._now!r}"
            )
            return
        self._next_deadline = self._now + self._interval
        self._delivered += 1
        if self._fail_next:
            # An ordinary callback failure disables the channel for the rest of
            # the run: no further notification, and no further activity either.
            self._is_stopped = True
            return
        self._rendered += 1

    @rule()
    def stop(self) -> None:
        """Stop the watchdog, the way any run teardown does."""
        self._armed().stop()
        self._is_stopped = True

    @invariant()
    def schedule_matches_the_model(self) -> None:
        """Check that the schedule's timestamps equal the model's."""
        schedule = self._schedule()
        fields = (
            schedule.started_at,
            schedule.last_activity,
            schedule.next_deadline,
            schedule.is_stopped,
        )
        expected = (
            self._started_at,
            self._last_activity,
            self._next_deadline,
            self._is_stopped,
        )
        assert fields == expected, (
            f"schedule {fields!r} drifted from the model {expected!r}"
        )

    @invariant()
    def arming_matches_the_model(self) -> None:
        """Check that the reported arm state agrees with the model's."""
        is_running = self._armed().is_running
        assert is_running is (not self._is_stopped), (
            "arm state must match the model for "
            f"stopped={self._is_stopped}, is_running={is_running}"
        )

    @invariant()
    def every_rendered_notification_wrote_one_line(self) -> None:
        """Check that each notification the renderer took wrote one line."""
        assert self._lines_written() == self._rendered, (
            "the sink must hold exactly one line per rendered notification for "
            f"rendered={self._rendered}, sink={self._sink.getvalue()!r}"
        )

    @invariant()
    def reporting_stays_bounded(self) -> None:
        """Check that no due notification is reported as still pending."""
        assert self._schedule().seconds_until_due(self._now) >= 0.0, (
            "a due notification must not be reported as still pending for "
            f"now={self._now!r}, deadline={self._next_deadline!r}"
        )


TestIdleHeartbeat = _IdleHeartbeatMachine.TestCase
TestIdleHeartbeat.settings = settings(
    max_examples=40,
    stateful_step_count=20,
    deadline=None,
)
