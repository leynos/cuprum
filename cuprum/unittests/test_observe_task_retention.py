"""Pruning and capacity rules for retained async observe-hook tasks.

An observed run schedules one task per awaitable observe hook per event and
retains them until finalization. These tests pin the rules that keep that list
bounded: which tasks are dropped, which failure is kept, and how line
observation waits for a slot. The end-to-end backpressure behaviour against a
real child lives in ``test_line_observe_backpressure.py``.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum._line_callbacks import (
    _compose_line_callbacks,
    _LineEmissionContext,
)
from cuprum._observability import _wait_for_exec_hook_tasks
from cuprum._observe_task_retention import (
    _await_hook_capacity,
    _has_hook_capacity,
    _prune_finished_hook_tasks,
)
from cuprum.unittests.test_line_event_emission_support import _make_observation

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent
    from cuprum.lines import LineEvent


class _HookFailureError(Exception):
    """Raised by a deliberately failing observe hook."""

    def __init__(self, marker: str) -> None:
        """Record the marker that identifies this failure."""
        super().__init__(marker)
        self.marker = marker


async def _succeed() -> None:
    """Complete without doing anything."""


async def _fail(marker: str) -> None:  # ruff: ignore[unused-async] - must be awaitable
    """Fail with a marker identifying the case."""
    raise _HookFailureError(marker)


async def _hold(gate: asyncio.Event) -> None:
    """Stay unfinished until ``gate`` opens, then complete successfully."""
    await gate.wait()


def _spawn(
    kind: str, gate: asyncio.Event, *, marker: str = "boom"
) -> asyncio.Task[None]:
    """Build one task in the requested outcome class for a pruning case."""
    if kind == "ok":
        return asyncio.create_task(_succeed())
    if kind == "fail":
        return asyncio.create_task(_fail(marker))
    if kind == "cancel":
        task = asyncio.create_task(_hold(gate))
        task.cancel()
        return task
    return asyncio.create_task(_hold(gate))


def test_prune_drops_successes_and_keeps_unfinished_tasks() -> None:
    """A settled success frees its slot; an unfinished task keeps its place."""

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        finished = asyncio.create_task(_succeed())
        unfinished = asyncio.create_task(_hold(gate))
        await asyncio.sleep(0)
        pending = [finished, unfinished]
        identity = id(pending)

        _prune_finished_hook_tasks(pending)

        assert pending == [unfinished], "only the unfinished task may remain"
        assert id(pending) == identity, "pruning must mutate the list in place"
        assert not unfinished.done(), "pruning must not settle unfinished tasks"
        gate.set()
        await _wait_for_exec_hook_tasks(pending)

    asyncio.run(case())


def test_prune_keeps_only_the_earliest_failure() -> None:
    """The final drain reports one failure, so later failures are discarded."""
    failure_order: list[str] = []

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        success = asyncio.create_task(_succeed())
        unfinished = asyncio.create_task(_hold(gate))
        first = asyncio.create_task(_fail("first"))
        second = asyncio.create_task(_fail("second"))
        await asyncio.sleep(0)
        failures = [success, unfinished, first, second]

        _prune_finished_hook_tasks(failures)

        assert failures == [unfinished, first], (
            "pruning must keep the earliest failure in list order"
        )
        gate.set()
        with pytest.raises(_HookFailureError) as exc_info:
            await _wait_for_exec_hook_tasks(failures)
        failure_order.append(exc_info.value.marker)

    asyncio.run(case())
    assert failure_order == ["first"], "finalization must report the kept failure"


def test_prune_keeps_the_earliest_cancelled_task() -> None:
    """Cancellation is reported like a failure, so the earliest one is kept."""

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        success = asyncio.create_task(_succeed())
        cancelled = _spawn("cancel", gate)
        failure = asyncio.create_task(_fail("later"))
        await asyncio.sleep(0)
        tasks = [success, cancelled, failure]

        _prune_finished_hook_tasks(tasks)

        assert tasks == [cancelled], "only the earliest cancellation may remain"
        assert tasks[0].cancelled(), "the kept task must still be cancelled"
        with pytest.raises(asyncio.CancelledError):
            await _wait_for_exec_hook_tasks(tasks)

    asyncio.run(case())


def test_prune_retrieves_dropped_exceptions() -> None:
    """Dropped failures must not surface as never-retrieved task warnings."""
    reports: list[dict[str, object]] = []

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: reports.append(context)
        )
        tasks = [asyncio.create_task(_fail(f"failure-{index}")) for index in range(5)]
        await asyncio.sleep(0)
        try:
            _prune_finished_hook_tasks(tasks)
            assert len(tasks) == 1, "one failure must be kept for reporting"
        finally:
            # Drop every reference so an unretrieved exception would be
            # reported when the loop collects the task.
            tasks.clear()
            gc.collect()
            await asyncio.sleep(0)

    asyncio.run(case())
    messages = [typ.cast("str", report.get("message", "")) for report in reports]
    assert not [message for message in messages if "never retrieved" in message], (
        f"pruning must retrieve dropped failures, got {messages!r}"
    )


def test_has_capacity_below_the_ceiling_does_not_prune() -> None:
    """The fast path must report capacity without touching settled tasks."""

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        finished = asyncio.create_task(_succeed())
        await asyncio.sleep(0)
        pending = [finished]

        assert _has_hook_capacity(pending, 3) is True, (
            "a list below the ceiling must report capacity"
        )
        assert pending == [finished], "the fast path must not mutate the list"
        gate.set()

    asyncio.run(case())


def test_has_capacity_prunes_settled_tasks_at_the_ceiling() -> None:
    """Reaching the ceiling prunes before the slot count is judged."""

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        finished = asyncio.create_task(_succeed())
        unfinished = asyncio.create_task(_hold(gate))
        await asyncio.sleep(0)
        pending = [finished, unfinished]

        assert _has_hook_capacity(pending, 2) is True, (
            "pruning at the ceiling must free the settled slot"
        )
        assert pending == [unfinished], "the settled task must have been pruned"
        gate.set()
        await _wait_for_exec_hook_tasks(pending)

    asyncio.run(case())


def test_has_capacity_reports_saturation_of_unfinished_tasks() -> None:
    """Only unfinished tasks count against the ceiling."""

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        pending = [asyncio.create_task(_hold(gate)) for _ in range(2)]
        await asyncio.sleep(0)

        assert _has_hook_capacity(pending, 2) is False, (
            "two unfinished tasks must saturate a ceiling of two"
        )
        assert len(pending) == 2, "saturation must retain every unfinished task"
        gate.set()
        await _wait_for_exec_hook_tasks(pending)

    asyncio.run(case())


def test_await_hook_capacity_returns_when_a_task_settles() -> None:
    """The waiter is released by one completing task, not by all of them."""

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        pending = [asyncio.create_task(_hold(gate)) for _ in range(3)]
        waiter = asyncio.create_task(_await_hook_capacity(pending, 3))
        await asyncio.sleep(0)

        assert not waiter.done(), "the waiter must block while every slot is held"
        gate.set()
        await asyncio.wait_for(waiter, timeout=5.0)
        await _wait_for_exec_hook_tasks(pending)

    asyncio.run(case())


def test_cancelling_the_waiter_leaves_observer_tasks_running() -> None:
    """Cancelling the wait must not cancel the tasks the execution owns."""

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        pending = [asyncio.create_task(_hold(gate)) for _ in range(2)]
        waiter = asyncio.create_task(_await_hook_capacity(pending, 2))
        await asyncio.sleep(0)

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert not any(task.cancelled() for task in pending), (
            "a cancelled waiter must not cancel observer tasks"
        )
        gate.set()
        await _wait_for_exec_hook_tasks(pending)

    asyncio.run(case())


def _describe(exc: BaseException) -> tuple[str, str | None]:
    """Render the part of a failure that finalization is expected to report."""
    if isinstance(exc, _HookFailureError):
        return ("failure", exc.marker)
    if isinstance(exc, asyncio.CancelledError):
        return ("cancelled", None)
    return (type(exc).__name__, str(exc))


async def _selected_failure(
    spec: list[str], *, prune: bool
) -> tuple[str, str | None] | None:
    """Return the outcome finalization reports for one outcome sequence."""
    gate = asyncio.Event()
    tasks = [
        _spawn(kind, gate, marker=f"failure-{index}") for index, kind in enumerate(spec)
    ]
    await asyncio.sleep(0)
    if prune:
        _prune_finished_hook_tasks(tasks)
    gate.set()
    try:
        await _wait_for_exec_hook_tasks(tasks)
    except BaseException as exc:  # ruff: ignore[blind-except] - selecting is the test
        return _describe(exc)
    return None


@settings(max_examples=40, deadline=None)
@given(spec=st.lists(st.sampled_from(("ok", "fail", "cancel", "pending")), max_size=6))
def test_pruning_preserves_the_failure_finalization_reports(
    spec: list[str],
) -> None:
    """Pruning must select exactly the failure the unpruned drain selects."""
    unpruned = asyncio.run(_selected_failure(spec, prune=False))
    pruned = asyncio.run(_selected_failure(spec, prune=True))

    assert pruned == unpruned, (
        f"pruning changed the reported outcome for {spec!r}: {pruned!r} != {unpruned!r}"
    )


class _RecordingHook:
    """Async observe hook that records each line and waits on a shared gate."""

    def __init__(self, gate: asyncio.Event) -> None:
        """Bind the gate every scheduled task waits on."""
        self.gate = gate
        self.seen: list[str] = []

    def __call__(self, event: ExecEvent) -> cabc.Awaitable[None] | None:
        """Record one line and defer its completion to the gate."""
        if event.phase not in {"stdout", "stderr"}:
            return None
        self.seen.append(typ.cast("str", event.line))
        return self._settle()

    async def _settle(self) -> None:
        """Complete when the test releases the gate."""
        await self.gate.wait()


class _ExplodingHook:
    """Synchronous observe hook that always fails."""

    def __call__(self, event: ExecEvent) -> None:
        """Fail without scheduling anything."""
        raise _HookFailureError(f"sync-{event.phase}")


def _line_context(
    on_line: cabc.Callable[[object], object] | None = None,
) -> _LineEmissionContext:
    """Build the per-stream context the composed callback reads."""
    return _LineEmissionContext(
        stream="stdout",
        pid=1,
        on_line=typ.cast("typ.Any", on_line),
        started_at=0.0,
        monotonic_clock=lambda: 1.0,
    )


def test_composed_callback_defers_observation_at_the_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Below the ceiling dispatch is inline; at it the line waits for a slot."""
    monkeypatch.setattr("cuprum._line_callbacks._OBSERVE_TASK_CEILING", 2)

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        hook = _RecordingHook(gate)
        observation = _make_observation((hook,))
        context = _line_context()
        emit = _compose_line_callbacks(observation, context)
        assert emit is not None

        assert emit("line-0") is None, "an available slot must not suspend"
        assert len(observation.pending_tasks) == 1, (
            "the first line must schedule exactly one observer task"
        )
        assert emit("line-1") is None, "the last slot must still dispatch inline"
        assert len(observation.pending_tasks) == 2, (
            "the second line must schedule a second observer task"
        )

        deferred = emit("line-2")
        assert inspect.isawaitable(deferred), "saturation must return an awaitable"
        assert len(observation.pending_tasks) == 2, (
            "a deferred line must not schedule its task before capacity exists"
        )

        gate.set()
        await deferred
        assert hook.seen == ["line-0", "line-1", "line-2"], (
            "every line must be delivered exactly once in order"
        )
        assert len(observation.pending_tasks) == 1, (
            "settled tasks must be pruned when the deferred line dispatches"
        )
        await _wait_for_exec_hook_tasks(observation.pending_tasks)

    asyncio.run(case())


class _StepClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        """Start the clock at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the current reading."""
        return self.now


def test_deferred_line_keeps_its_decode_time_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waiting for capacity must not shift a line's ``at`` stamp."""
    monkeypatch.setattr("cuprum._line_callbacks._OBSERVE_TASK_CEILING", 1)

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        observation = _make_observation((_RecordingHook(gate),))
        clock = _StepClock()
        stamped: list[LineEvent] = []
        context = _LineEmissionContext(
            stream="stdout",
            pid=1,
            on_line=stamped.append,
            started_at=0.0,
            monotonic_clock=clock,
        )
        emit = _compose_line_callbacks(observation, context)
        assert emit is not None

        clock.now = 2.0
        assert emit("line-0") is None, "the first line must dispatch inline"
        clock.now = 5.0
        deferred = emit("line-1")
        assert inspect.isawaitable(deferred), "saturation must return an awaitable"

        clock.now = 90.0
        gate.set()
        await deferred

        assert [event.at for event in stamped] == [2.0, 5.0], (
            "a deferred line must report when it was decoded, not when its "
            "callback finally ran"
        )
        await _wait_for_exec_hook_tasks(observation.pending_tasks)

    asyncio.run(case())


def test_two_hooks_overshoot_the_ceiling_by_at_most_hooks_minus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capacity is checked per event, so one event may overshoot by its hooks."""
    monkeypatch.setattr("cuprum._line_callbacks._OBSERVE_TASK_CEILING", 3)

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        first = _RecordingHook(gate)
        second = _RecordingHook(gate)
        observation = _make_observation((first, second))
        emit = _compose_line_callbacks(observation, _line_context())
        assert emit is not None

        assert emit("a") is None, "the first line must dispatch inline"
        assert emit("b") is None, "the second line must dispatch inline"
        assert len(observation.pending_tasks) == 4, (
            "two events of two hooks may overshoot a ceiling of three by one"
        )
        assert len(observation.pending_tasks) <= 3 + 2 - 1, (
            "overshoot must stay within ceiling plus hooks minus one"
        )

        deferred = emit("c")
        assert inspect.isawaitable(deferred), "the next event must wait for a slot"
        assert len(observation.pending_tasks) == 4, "no task may be added while full"

        gate.set()
        await deferred
        assert first.seen == ["a", "b", "c"], (
            "the first hook must see every line exactly once, in order"
        )
        assert second.seen == ["a", "b", "c"], (
            "the second hook must see every line exactly once, in order"
        )
        await _wait_for_exec_hook_tasks(observation.pending_tasks)

    asyncio.run(case())


def test_on_line_does_not_run_when_observation_dispatch_fails() -> None:
    """A failing observe hook must not let the caller's hook see the line."""
    called: list[object] = []

    def on_line(event: object) -> None:
        """Record that the caller hook was invoked."""
        called.append(event)

    observation = _make_observation((_ExplodingHook(),))
    emit = _compose_line_callbacks(observation, _line_context(on_line))
    assert emit is not None

    with pytest.raises(_HookFailureError):
        _ = emit("line")

    assert not called, "on_line must not run after a failed observation"
    assert not observation.pending_tasks, "a sync failure schedules no task"
