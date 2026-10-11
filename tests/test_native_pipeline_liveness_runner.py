"""Contract tests for the runner that bounds an attempt on observed progress.

The policy and the monitor are pinned by example in
:mod:`tests.test_native_pipeline_liveness`; what no test reached was the
composition of :func:`~tests.behaviour._native_pipeline_hand_off._run_liveness_bounded`
with the verdict application in
:meth:`~tests.behaviour._native_pipeline_hand_off._NativePipelineHandOff.run_attempt`.
A runner that bypassed the monitor, never cancelled a stalled pipeline, or
always waited for ``pipeline.run()`` would have passed every other test in the
repository: the only live caller is a Linux-gated behaviour test that exercises
the healthy path alone.

A controllable pipeline supplies the missing control. The runner needs exactly
one operation from a pipeline, so a double implements that one method. One clock
is shared by the tracker and the bound, so a stall is produced by moving that
clock rather than by waiting for one, and no assertion depends on elapsed time.
"""

from __future__ import annotations

import asyncio
import threading
import typing as typ

import pytest

from cuprum._backend import StreamBackend
from cuprum.sh import PipelineResult
from cuprum.unittests._rust_pump_test_helpers import ControllableMonotonicClock
from tests.behaviour import _native_pipeline_hand_off as hand_off_module
from tests.behaviour._native_pipeline_hand_off import (
    _NativePipelineHandOff,
    _ProgressTracker,
    _run_liveness_bounded,
)
from tests.behaviour._native_pipeline_liveness import LivenessBound
from tests.behaviour._native_pipeline_stall import _StallSnapshot
from tests.helpers.native_pipeline_doubles import (
    CompletingPipeline,
    StallingPipeline,
    as_pipeline,
    pipeline_result,
)
from tests.helpers.timeouts import pending_tasks

if typ.TYPE_CHECKING:
    from cuprum.program import Program
    from cuprum.sh import Pipeline

#: The interval the doubles are driven with; only the runner moves a test
#: between the two bounds.
_INTERVAL_S = 0.5
_EPOCH_S = 0.0
#: An instant well past the interval, so the monitor settles on its next pass.
_QUIET_AT_S = 2.0
#: An instant still inside the interval, but past an already-spent backstop.
_SPENT_DEADLINE_S = 0.3
_PROGRESSING_AT_S = 0.4
#: A pid above the default ceiling, so no live process can ever answer for it.
_UNREACHABLE_PID = 2**22 + 1

#: The one clock every tracker and bound in this module reads. A stall is
#: produced by moving it, which is why nothing here waits on wall time.
_CLOCK = ControllableMonotonicClock()


def _stalling(*, stall_at: float) -> StallingPipeline:
    """Build a stalling double that moves this module's shared clock."""
    return StallingPipeline(clock=_CLOCK, stall_at=stall_at)


def _tracker() -> _ProgressTracker:
    """Build the tracker the runner would build, on the shared clock."""
    return _ProgressTracker(clock=_CLOCK)


def _seeded_tracker() -> _ProgressTracker:
    """Build a tracker already holding a child the classifier can read.

    The seeded pid is above the pid ceiling, so the probe reports a child that
    has exited. An exited child beside a pending task is exactly the evidence
    the pending-task rule reads, so this is what makes a stalled attempt
    classify as a missed hand-off rather than as host starvation.

    Returns
    -------
    _ProgressTracker
        A tracker on the shared clock, already tracking the seeded pid.
    """
    tracker = _tracker()
    tracker.pids.add(_UNREACHABLE_PID)
    return tracker


def _bound(*, deadline: float | None = None) -> LivenessBound:
    """Return a bound reading the shared clock, with an optional backstop."""
    return LivenessBound(
        no_progress_interval_s=_INTERVAL_S,
        aggregate_deadline=deadline,
        monotonic_clock=_CLOCK,
    )


def _hand_off(
    double: object,
    *,
    bound: LivenessBound,
    baseline_threads_ago: int = 0,
) -> _NativePipelineHandOff:
    """Build one hand-off configuration whose every attempt uses ``double``.

    ``baseline_threads_ago`` records the baseline thread count as it stood
    *before* this many threads existed, which is what makes the measured delta
    a value the test chose. Without it the double spawns nothing, the delta is
    zero, and an assertion of ``thread_delta=0`` could be satisfied by a
    formatter that had stopped reading the measurement at all.

    Returns
    -------
    _NativePipelineHandOff
        The hand-off configuration, with its thread baseline wound back by
        ``baseline_threads_ago``.
    """

    def make_pipeline(_code: str) -> tuple[Pipeline, frozenset[Program]]:
        """Return the same double for every attempt, allowlisting nothing."""
        return as_pipeline(double), frozenset()

    return _NativePipelineHandOff(
        StreamBackend.AUTO,
        make_pipeline,
        bound,
        threading.active_count() - baseline_threads_ago,
    )


@pytest.fixture(autouse=True)
def _restore_clock() -> None:
    """Return the shared clock to its epoch before each test."""
    _CLOCK.advance_to(_EPOCH_S)


@pytest.fixture
def _runner_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the runner's own tracker read the shared clock.

    ``run_attempt`` constructs its tracker itself, so the clock seam is closed
    by substituting the tracker the runner builds rather than by widening the
    runner's signature for a test.
    """
    monkeypatch.setattr(hand_off_module, "_ProgressTracker", _tracker)


@pytest.fixture
def _seeded_runner_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    """As :func:`_runner_tracker`, but the tracker already holds a child."""
    monkeypatch.setattr(hand_off_module, "_ProgressTracker", _seeded_tracker)


def test_a_quiet_monitor_stops_a_stalled_pipeline() -> None:
    """A stall is captured and the pipeline task is cancelled and awaited."""
    pipeline = _stalling(stall_at=_QUIET_AT_S)

    outcome = asyncio.run(
        _run_liveness_bounded(as_pipeline(pipeline), _tracker(), _bound()),
    )

    assert isinstance(outcome, _StallSnapshot), (
        f"a stalling pipeline under a quiet clock must be captured, got {outcome!r}"
    )
    assert outcome.reason == "no observable pipeline progress", (
        f"the quiet bound must be the one that fired, got {outcome.reason!r}"
    )
    assert pipeline.cancelled, (
        "the stalled pipeline task must be cancelled before the runner returns, "
        "or the attempt leaks a live task into the next one"
    )


def test_a_quiet_monitor_leaves_no_pending_tasks() -> None:
    """The cancelled pipeline task is awaited rather than abandoned."""
    pipeline = _stalling(stall_at=_QUIET_AT_S)

    async def drive() -> tuple[_StallSnapshot, set[asyncio.Task[object]]]:
        """Run the bounded attempt and report what the loop still holds."""
        outcome = await _run_liveness_bounded(
            as_pipeline(pipeline), _tracker(), _bound()
        )
        assert isinstance(outcome, _StallSnapshot), (
            f"a stalled attempt must report a snapshot, got {outcome!r}"
        )
        return outcome, pending_tasks()

    outcome, outstanding = asyncio.run(drive())

    assert outcome.reason == "no observable pipeline progress", (
        "the quiet bound, not the backstop, must be the one that fired here, "
        f"got {outcome.reason!r}"
    )
    assert not outstanding, (
        "both the pipeline and the monitor task must be awaited, found "
        f"{[task.get_name() for task in outstanding]!r}"
    )


def test_a_spent_backstop_stops_a_still_progressing_pipeline() -> None:
    """The backstop is reached only while the observation stream is unquiet."""
    pipeline = _stalling(stall_at=_PROGRESSING_AT_S)

    outcome = asyncio.run(
        _run_liveness_bounded(
            as_pipeline(pipeline),
            _tracker(),
            _bound(deadline=_SPENT_DEADLINE_S),
        ),
    )

    assert isinstance(outcome, _StallSnapshot), (
        f"a progressing run past its deadline must reach the backstop, got {outcome!r}"
    )
    assert outcome.reason == "aggregate suite-safety backstop", (
        "the quiet period is tested first and wins ties, so a progress clock "
        f"still inside the interval must route to the backstop, got "
        f"{outcome.reason!r}"
    )
    assert pipeline.cancelled, "the backstop path must cancel the pipeline too"


def test_a_completing_pipeline_cancels_the_monitor() -> None:
    """A finished attempt returns its result and stops watching progress."""
    outstanding: set[asyncio.Task[object]] = set()

    async def drive() -> PipelineResult | _StallSnapshot:
        """Run the attempt and report the tasks the loop still holds."""
        nonlocal outstanding
        outcome = await _run_liveness_bounded(
            as_pipeline(CompletingPipeline()), _tracker(), _bound()
        )
        outstanding = pending_tasks()
        return outcome

    outcome = asyncio.run(drive())

    assert isinstance(outcome, PipelineResult), (
        f"a completed attempt is not a stall, got {outcome!r}"
    )
    assert outcome.stdout == "HELLO", (
        f"the runner must return the pipeline's own result, got {outcome!r}"
    )
    assert not outstanding, (
        "a completed attempt must not strand the monitor task, found "
        f"{[task.get_name() for task in outstanding]!r}"
    )


@pytest.mark.usefixtures("_runner_tracker")
def test_a_healthy_attempt_is_accepted_by_the_runner() -> None:
    """The healthy path returns instead of failing or skipping the attempt."""
    hand_off = _hand_off(CompletingPipeline(), bound=_bound())

    hand_off.run_attempt(0, 0)


@pytest.mark.usefixtures("_runner_tracker")
def test_the_attempt_fails_when_pipeline_output_is_lost() -> None:
    """A completed attempt that lost its output is still a failure."""
    hand_off = _hand_off(
        CompletingPipeline(pipeline_result(stdout=None)), bound=_bound()
    )

    with pytest.raises(pytest.fail.Exception) as failure:
        hand_off.run_attempt(5, 1)

    assert "lost pipeline output" in str(failure.value), (
        f"a completed attempt without HELLO must fail, got {failure.value!s}"
    )
    assert "attempt 5" in str(failure.value), (
        f"the failure must name the attempt it came from, got {failure.value!s}"
    )


@pytest.mark.usefixtures("_runner_tracker")
def test_the_attempt_fails_on_a_non_zero_stage() -> None:
    """A completed attempt whose stage failed is reported as such."""
    hand_off = _hand_off(
        CompletingPipeline(pipeline_result(exit_code=1)), bound=_bound()
    )

    with pytest.raises(pytest.fail.Exception) as failure:
        hand_off.run_attempt(0, 0)

    assert "non-zero stage" in str(failure.value), (
        f"a failed stage must fail the attempt, got {failure.value!s}"
    )


@pytest.mark.usefixtures("_seeded_runner_tracker")
def test_a_hung_hand_off_fails_the_attempt() -> None:
    """A stall evidencing a missed hand-off is applied as a failure."""
    hand_off = _hand_off(_stalling(stall_at=_QUIET_AT_S), bound=_bound())

    with pytest.raises(pytest.fail.Exception) as failure:
        hand_off.run_attempt(0, 0)

    message = str(failure.value)
    assert "verdict=HUNG_HANDOFF" in message, (
        "a child that exited beside a pending task must be reported as a "
        f"missed hand-off, got {message!r}"
    )
    assert f"pid={_UNREACHABLE_PID}" in message, (
        f"the failure must carry the evidence it was classified from, got {message!r}"
    )


@pytest.mark.usefixtures("_runner_tracker")
def test_a_host_starvation_stall_skips_the_attempt() -> None:
    """A stall with no positive evidence of a missed close is skipped."""
    hand_off = _hand_off(_stalling(stall_at=_QUIET_AT_S), bound=_bound())

    with pytest.raises(pytest.skip.Exception) as skipped:
        hand_off.run_attempt(0, 0)

    assert "verdict=HOST_STARVATION" in str(skipped.value), (
        "a stall the classifier cannot attribute must skip rather than fail, "
        f"got {skipped.value!s}"
    )


@pytest.mark.usefixtures("_seeded_runner_tracker")
@pytest.mark.parametrize(
    ("attempt", "fd_delta", "baseline_threads_ago", "fragment"),
    [
        pytest.param(7, 3, 0, "attempt=7", id="attempt"),
        pytest.param(7, 3, 0, "fd_delta=3", id="fd_delta"),
        pytest.param(7, 3, 4, "thread_delta=4", id="thread_delta"),
    ],
)
def test_the_attempt_report_carries_the_attempt_and_deltas(
    attempt: int, fd_delta: int, baseline_threads_ago: int, fragment: str
) -> None:
    """The report hands the diagnostic the attempt index and both deltas.

    The diagnostics are only as useful as the fields ``run_attempt`` fills in,
    so the attempt index, the descriptor delta, and the thread delta are
    asserted rather than assumed. The thread delta is measured against the
    count taken when the hand-off was configured, so a double that spawns
    nothing reports exactly the baseline the test wound back.

    Each field is probed by its own invocation, so a failure names the field
    that stopped being reported rather than the first of three that happened to
    be checked. Every value is one this test chose rather than a zero the
    fixture would have produced anyway: the attempt is not the first attempt,
    the descriptor delta is not flat, and the thread baseline is wound back so
    the measured delta is a non-zero count the diagnostic has to carry through.
    """
    hand_off = _hand_off(
        _stalling(stall_at=_QUIET_AT_S),
        bound=_bound(),
        baseline_threads_ago=baseline_threads_ago,
    )

    with pytest.raises(pytest.fail.Exception) as failure:
        hand_off.run_attempt(attempt, fd_delta)

    message = str(failure.value)
    assert fragment in message, (
        f"the report must carry {fragment!r} from run_attempt's arguments, "
        f"got {message!r}"
    )
