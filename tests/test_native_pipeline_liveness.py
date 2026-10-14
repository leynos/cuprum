"""Contract tests for the native hand-off liveness policy and its runner.

The liveness policy is clock arithmetic, so it can be verified without a
pipeline: a stalled run is modelled by moving the progress clock rather than by
waiting for one. The runner is then driven to both of its bounds — an
observation stream that has gone quiet, and an observation stream that keeps
progressing until the suite-safety backstop expires — so the distinction the
policy exists to make is pinned end to end.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import typing as typ

import pytest

from cuprum.events import ExecEvent
from cuprum.program import Program

# The in-tree clock double the heartbeat tests already drive, aliased under the
# name this module has always used for it. The policy is arithmetic over a
# clock, so a test that read the real one would make an assertion such as "an
# event advanced the tracked time" depend on how long the two statements took —
# a bound any descheduling longer than the interval would violate, on a host
# where other builds compete for the CPU. Reusing the shared double keeps a
# single implementation in step with the tracker both modules feed.
from cuprum.unittests._rust_pump_test_helpers import (
    ControllableMonotonicClock as _TestClock,
)
from tests.behaviour._native_pipeline_hand_off import (
    _capture_stall,
    _monitor_progress,
    _ProgressTracker,
    _StallSnapshot,
)
from tests.behaviour._native_pipeline_liveness import (
    NO_PROGRESS_INTERVAL_S,
    PROGRESS_PROBE_INTERVAL_S,
    STAGE_INDEX_TAG,
    LivenessBound,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecPhase

#: Far enough past the interval that no sampling delay can close the gap, so a
#: test asserting "quiet" cannot fail for want of elapsed time.
_QUIET = NO_PROGRESS_INTERVAL_S * 10


def _lifecycle_event(
    phase: str,
    *,
    pid: int | None = None,
    tags: cabc.Mapping[str, object] | None = None,
) -> ExecEvent:
    """Build a real ``ExecEvent`` carrying the fields the tracker reads.

    The tracker consumes genuine events on the public observe-hook seam, so its
    tests feed it the real type rather than a stand-in: a stub free to drift
    from ``ExecEvent`` would let a change to the field set pass unnoticed.

    Returns
    -------
    ExecEvent
        An event whose phase, pid, and tags are those the tracker inspects.
    """
    return ExecEvent(
        phase=typ.cast("ExecPhase", phase),
        program=Program("cat"),
        argv=("cat",),
        cwd=None,
        env=None,
        pid=pid,
        timestamp=0.0,
        line=None,
        exit_code=None,
        duration_s=None,
        tags={} if tags is None else tags,
    )


def _quiet_tracker() -> tuple[_ProgressTracker, LivenessBound]:
    """Return a stalled run: a tracker and the bound that read its clock.

    The two share one test clock, so "quiet" is a value this fixture chooses
    rather than a duration a test waits out.

    Returns
    -------
    tuple[_ProgressTracker, LivenessBound]
        The tracker, and a bound reading the same clock.
    """
    clock = _TestClock()
    tracker = _ProgressTracker(clock=clock)
    clock.advance_to(_QUIET)
    return tracker, LivenessBound(monotonic_clock=clock)


def test_a_controllable_clock_drives_the_policy_without_waiting() -> None:
    """The bound reads its injected clock, so a stall needs no elapsed time."""
    clock = _TestClock()
    tracker = _ProgressTracker(clock=clock)
    bound = LivenessBound(monotonic_clock=clock)

    assert not bound.settled(tracker.last_progress_at), (
        "a tracker primed from the clock must start inside the interval"
    )

    clock.advance_to(NO_PROGRESS_INTERVAL_S * 2)

    assert bound.settled(tracker.last_progress_at), (
        "moving the clock past the interval must settle the bound, with no "
        "wall-clock wait"
    )


def test_advancing_the_clock_to_an_instant_does_not_accumulate() -> None:
    """The clock lands on the instant it was given, not the sum of its moves.

    Absolute placement is what lets a fixture reset a shared clock to an epoch
    between tests, and what lets a repeated attempt land on the same stall
    instant every time instead of drifting with the attempt count. Placement is
    asserted by moving the clock twice and requiring the *second* value alone:
    a clock that accumulated would report the sum here.
    """
    clock = _TestClock()

    clock.advance_to(11.5)
    clock.advance_to(2.5)

    assert clock() == pytest.approx(2.5), (
        f"advance_to must place the clock, not add to it, got {clock()!r}"
    )


def test_the_injected_clock_is_what_the_observe_hook_stamps() -> None:
    """An observed event is stamped from the tracker's own clock, not the real one."""
    clock = _TestClock()
    tracker = _ProgressTracker(clock=clock)
    clock.advance_to(42.0)

    tracker.observe(_lifecycle_event("stdout"))

    assert tracker.last_progress_at == pytest.approx(42.0), (
        f"the event must be stamped from the injected clock, got "
        f"{tracker.last_progress_at!r}"
    )


def test_a_real_clock_is_the_default_seam() -> None:
    """Both seams default to the real clock, so callers need not supply one."""
    tracker = _ProgressTracker()

    assert tracker.last_progress_at == pytest.approx(time.monotonic(), abs=1.0), (
        "a tracker built without a clock must still be primed from the real one"
    )
    assert LivenessBound().monotonic_clock is time.monotonic, (
        "the bound must default to the shipped clock"
    )


def test_the_default_bound_carries_the_shipped_interval() -> None:
    """The bound defaults to the policy constants rather than to a copy."""
    bound = LivenessBound()

    assert bound.no_progress_interval_s == NO_PROGRESS_INTERVAL_S, (
        "the bound must default to the shipped no-progress interval"
    )
    assert not bound.settled(time.monotonic()), (
        "a bound with no aggregate deadline must still bound an attempt"
    )


def test_a_quiet_tracker_reaches_the_quiet_bound() -> None:
    """A tracker that has emitted nothing for the interval is stalled."""
    tracker, bound = _quiet_tracker()

    assert bound.settled(tracker.last_progress_at), (
        "a tracker whose clock has been still must be reported as stalled"
    )


def test_the_bound_reports_progress_and_remaining_backstop_together() -> None:
    """The captured progress describes both clocks at one sampling instant.

    Both numbers are read from the same instant, so the fixture sets that
    instant rather than waiting for one: a real clock would make the reported
    quiet period and the remaining backstop depend on when they were read.
    """
    clock = _TestClock()
    bound = LivenessBound(aggregate_deadline=_QUIET + 30.0, monotonic_clock=clock)
    clock.advance_to(_QUIET)

    progress = bound.progress(0.0)

    assert progress.quiet_for_s == _QUIET, (
        f"the quiet period must be reported as elapsed, got {progress!r}"
    )
    assert progress.deadline_remaining_s == pytest.approx(30.0), (
        f"the remaining backstop must be reported, got {progress!r}"
    )


def test_the_quiet_bound_outranks_an_elapsed_backstop() -> None:
    """A run that has gone quiet is classified from evidence, not the clock."""
    bound = LivenessBound(aggregate_deadline=time.monotonic() - 1.0)

    assert not bound.expired(time.monotonic() - _QUIET), (
        "an already-stalled run must reach the quiet bound, not the backstop"
    )


def test_an_elapsed_backstop_expires_a_still_progressing_run() -> None:
    """The backstop bounds a run that never stops making progress."""
    bound = LivenessBound(aggregate_deadline=time.monotonic() - 1.0)

    assert bound.expired(time.monotonic()), (
        "a progressing run past its aggregate deadline must reach the backstop"
    )


def test_the_pre_attempt_check_reports_the_spent_backstop() -> None:
    """A spent backstop before an attempt starts is reported as spent."""
    bound = LivenessBound(aggregate_deadline=time.monotonic() - 1.0)

    assert bound.pre_attempt_backstop_reached(), (
        "a spent backstop must be reported so the attempt skips unclassified"
    )


def test_the_pre_attempt_check_ignores_a_live_backstop() -> None:
    """A backstop with time left is not spent, however quiet the clock is.

    This is what separates the pre-attempt guard from :meth:`LivenessBound.expired`:
    it must not inherit that method's quiet-period tie-break, which would keep
    it from ever firing.
    """
    bound = LivenessBound(aggregate_deadline=time.monotonic() + 30.0)

    assert not bound.pre_attempt_backstop_reached(), (
        "a backstop with time left must let the next attempt run"
    )


def test_the_pre_attempt_check_needs_a_deadline() -> None:
    """An unbounded run has no backstop to reach."""
    assert not LivenessBound().pre_attempt_backstop_reached(), (
        "a bound with no aggregate deadline cannot have spent one"
    )


def test_no_progress_interval_is_larger_than_the_probe_interval() -> None:
    """The monitor must sample several times inside one quiet period."""
    assert PROGRESS_PROBE_INTERVAL_S < NO_PROGRESS_INTERVAL_S, (
        "a probe interval at or above the liveness interval would skip a stall"
    )
    ratio = NO_PROGRESS_INTERVAL_S / PROGRESS_PROBE_INTERVAL_S
    assert ratio >= 5, f"the liveness interval must allow several samples, got {ratio}"


def test_a_quiet_run_yields_a_stall_snapshot() -> None:
    """The monitor returns the captured evidence once progress stops."""
    tracker, bound = _quiet_tracker()

    snapshot = asyncio.run(_monitor_progress(tracker, bound))

    assert snapshot.reason == "no observable pipeline progress", (
        f"a quiet run must be captured as a stall, got {snapshot.reason!r}"
    )


def test_the_monitor_keeps_waiting_while_progress_is_recent() -> None:
    """A run that has just emitted an event is not yet a stall.

    The interval is widened far beyond the probe so that only a genuine change
    of behaviour, rather than a slow host, can make the monitor settle here.
    """
    lint_free = LivenessBound(no_progress_interval_s=NO_PROGRESS_INTERVAL_S * 100)

    assert not asyncio.run(_monitor_settled_within(_ProgressTracker(), lint_free)), (
        "a run whose progress clock just advanced must not be reported stalled"
    )


async def _monitor_settled_within(
    tracker: _ProgressTracker,
    bound: LivenessBound,
) -> bool:
    """Run the monitor briefly and report whether it captured a stall."""
    monitor = asyncio.create_task(_monitor_progress(tracker, bound))
    await asyncio.sleep(PROGRESS_PROBE_INTERVAL_S * 2)
    settled = monitor.done()
    monitor.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await monitor
    return settled


def test_an_event_advances_the_progress_clock() -> None:
    """The observe hook is what advances the tracked clock.

    Every instant is chosen by the test, so the assertion separates "the hook
    advanced the clock" from "enough real time passed". Read against the real
    clock the same assertion would fail whenever the two statements were more
    than one interval apart — a descheduling, on a host where other builds
    compete for the CPU, and not a behaviour change.
    """
    clock = _TestClock()
    tracker = _ProgressTracker(clock=clock)
    bound = LivenessBound(monotonic_clock=clock)
    clock.advance_to(_QUIET)

    assert bound.settled(tracker.last_progress_at), (
        "the run must be stalled before the hook is called, or the assertion "
        "below would pass for want of a fixture"
    )

    tracker.observe(_lifecycle_event("stdout"))

    assert not bound.settled(tracker.last_progress_at), (
        "a lifecycle event must advance the progress clock, and neither "
        "statement may move it by itself"
    )


def test_a_non_lifecycle_event_does_not_advance_the_clock() -> None:
    """An ancillary diagnostic is not evidence of pipeline progress."""
    clock = _TestClock()
    tracker = _ProgressTracker(clock=clock)
    clock.advance_to(_QUIET)

    tracker.observe(_lifecycle_event("timeout"))

    assert tracker.last_progress_at == pytest.approx(0.0), (
        "an ancillary diagnostic must leave the progress clock where it stood, "
        f"got {tracker.last_progress_at!r}"
    )


def test_the_observe_hook_records_the_stage_of_each_started_child() -> None:
    """A started child's tagged stage position is what the stall rules compare."""
    tracker = _ProgressTracker()

    tracker.observe(_lifecycle_event("start", pid=4242, tags={STAGE_INDEX_TAG: 3}))

    assert tracker.pids == {4242}, "a started child must be tracked by pid"
    assert tracker.stage_by_pid == {4242: 3}, (
        "a started child's tagged pipeline position must be recorded"
    )


def test_capture_stall_reports_the_children_and_pending_tasks() -> None:
    """The snapshot carries the tracked pids and the live task names.

    Stall capture runs inside the pipeline's event loop, so the pending-task
    query has a running loop to read; the coroutine below supplies one.
    """
    tracker = _ProgressTracker()
    tracker.pids.add(2**22 + 1)  # Above the default pid ceiling: never a process.

    snapshot = asyncio.run(_capture_on_a_running_loop(tracker))

    assert snapshot.reason == "test reason", "the reason must be carried through"
    assert [child.process.pid for child in snapshot.children] == [2**22 + 1], (
        f"every tracked pid must be snapshotted, got {snapshot.children!r}"
    )
    assert snapshot.task_names == (), (
        "the caller is the only task on the loop, and it excludes itself; "
        f"got {snapshot.task_names!r}"
    )


async def _capture_on_a_running_loop(tracker: _ProgressTracker) -> _StallSnapshot:
    """Capture a stall the way the monitor does: from inside a running loop."""
    await asyncio.sleep(0)
    return _capture_stall(tracker, "test reason")
