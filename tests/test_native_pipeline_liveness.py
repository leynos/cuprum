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
    quiet_for_s,
)

#: Far enough past the interval that no sampling delay can close the gap, so a
#: test asserting "quiet" cannot fail for want of elapsed time.
_QUIET = NO_PROGRESS_INTERVAL_S * 10


class _LifecycleEvent:
    """The subset of ``ExecEvent`` the progress tracker reads."""

    def __init__(
        self,
        phase: str,
        *,
        pid: int | None = None,
        tags: dict[str, object] | None = None,
    ) -> None:
        """Record the phase, optional pid, and optional tags."""
        self.phase = phase
        self.pid = pid
        self.tags = {} if tags is None else tags


def _quiet_tracker() -> _ProgressTracker:
    """Return a tracker whose progress clock went quiet long ago."""
    tracker = _ProgressTracker()
    tracker.last_progress_at = time.monotonic() - _QUIET
    return tracker


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
    assert LivenessBound().settled(_quiet_tracker().last_progress_at), (
        "a tracker whose clock has been still must be reported as stalled"
    )


def test_the_bound_reports_progress_and_remaining_backstop_together() -> None:
    """The captured progress describes both clocks at one sampling instant."""
    bound = LivenessBound(aggregate_deadline=time.monotonic() + 30.0)
    progress = bound.progress(time.monotonic() - _QUIET)

    assert progress.quiet_for_s >= _QUIET, (
        f"the quiet period must be reported as elapsed, got {progress!r}"
    )
    assert 0.0 < progress.deadline_remaining_s <= 30.0, (
        f"the remaining backstop must be positive and bounded, got {progress!r}"
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
    snapshot = asyncio.run(_monitor_progress(_quiet_tracker(), LivenessBound()))

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
    """The observe hook is what advances the tracked clock."""
    tracker = _quiet_tracker()

    tracker.observe(_LifecycleEvent("stdout"))  # type: ignore[arg-type]

    assert quiet_for_s(tracker.last_progress_at) < NO_PROGRESS_INTERVAL_S, (
        "a lifecycle event must advance the progress clock"
    )


def test_a_non_lifecycle_event_does_not_advance_the_clock() -> None:
    """An ancillary diagnostic is not evidence of pipeline progress."""
    tracker = _quiet_tracker()

    tracker.observe(_LifecycleEvent("timeout"))  # type: ignore[arg-type]

    assert quiet_for_s(tracker.last_progress_at) >= NO_PROGRESS_INTERVAL_S, (
        "an ancillary diagnostic must not be read as pipeline progress"
    )


def test_the_observe_hook_records_the_stage_of_each_started_child() -> None:
    """A started child's tagged stage position is what the stall rules compare."""
    tracker = _ProgressTracker()

    tracker.observe(  # type: ignore[arg-type]
        _LifecycleEvent("start", pid=4242, tags={STAGE_INDEX_TAG: 3})
    )

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
