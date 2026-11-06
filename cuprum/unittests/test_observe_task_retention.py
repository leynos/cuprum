"""Pruning and admission rules for retained async observe-hook tasks.

An observed run schedules one task per awaitable observe hook per event and
retains them until finalization. These tests pin the rules that keep that list
bounded: which tasks are dropped, which failure is kept, and how line
observation is admitted against the ceiling. The composed-callback behaviour
lives in ``test_observe_task_retention_callbacks.py``, the telemetry in
``test_observe_task_retention_telemetry.py``, the end-to-end backpressure
behaviour against a real child in ``test_line_observe_backpressure.py``, the
two-producer pipeline case in ``test_line_observe_pipeline_backpressure.py``,
and the child-write proof in ``test_line_observe_child_backpressure.py``.
"""

from __future__ import annotations

import asyncio
import gc
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum._line_stream import _LINE_QUEUE_CAPACITY
from cuprum._observability import _wait_for_exec_hook_tasks
from cuprum._observe_task_retention import (
    _OBSERVE_TASK_CEILING,
    _admit_hook_capacity,
    _await_hook_capacity,
    _prune_finished_hook_tasks,
)


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


def test_observe_task_ceiling_matches_line_queue_capacity() -> None:
    """The two per-run bounds describe one quantity, so neither may drift."""
    assert _OBSERVE_TASK_CEILING == _LINE_QUEUE_CAPACITY, (
        "the observe-task ceiling must match the line queue capacity: "
        f"{_OBSERVE_TASK_CEILING} != {_LINE_QUEUE_CAPACITY}"
    )


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

        pruned = _prune_finished_hook_tasks(pending)

        assert pruned == 1, "the settled success must be the only task dropped"
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


def test_admission_below_the_ceiling_does_not_prune() -> None:
    """The fast path must admit without touching settled tasks."""

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        finished = asyncio.create_task(_succeed())
        await asyncio.sleep(0)
        pending = [finished]

        assert _admit_hook_capacity(pending, 3) is True, (
            "a list below the ceiling must admit the next task"
        )
        assert pending == [finished], "the fast path must not mutate the list"
        gate.set()

    asyncio.run(case())


def test_admission_prunes_settled_tasks_at_the_ceiling() -> None:
    """Reaching the ceiling prunes before the slot count is judged."""

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        finished = asyncio.create_task(_succeed())
        unfinished = asyncio.create_task(_hold(gate))
        await asyncio.sleep(0)
        pending = [finished, unfinished]

        assert _admit_hook_capacity(pending, 2) is True, (
            "pruning at the ceiling must free the settled slot"
        )
        assert pending == [unfinished], "the settled task must have been pruned"
        gate.set()
        await _wait_for_exec_hook_tasks(pending)

    asyncio.run(case())


def test_admission_reports_saturation_of_unfinished_tasks() -> None:
    """Only unfinished tasks count against the ceiling."""

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gate = asyncio.Event()
        pending = [asyncio.create_task(_hold(gate)) for _ in range(2)]
        await asyncio.sleep(0)

        assert _admit_hook_capacity(pending, 2) is False, (
            "two unfinished tasks must saturate a ceiling of two"
        )
        assert len(pending) == 2, "saturation must retain every unfinished task"
        gate.set()
        await _wait_for_exec_hook_tasks(pending)

    asyncio.run(case())


def test_await_hook_capacity_returns_when_a_task_settles() -> None:
    """One completion must release the waiter while the others stay blocked."""

    async def case() -> None:
        """Exercise the scenario inside a running event loop."""
        gates = [asyncio.Event() for _ in range(3)]
        first, second, third = (asyncio.create_task(_hold(gate)) for gate in gates)
        pending = [first, second, third]
        waiter = asyncio.create_task(_await_hook_capacity(pending, 3))
        await asyncio.sleep(0)

        assert not waiter.done(), "the waiter must block while every slot is held"
        gates[1].set()
        await asyncio.wait_for(waiter, timeout=5.0)

        # Pruning mutates the shared list, so hold the task references rather
        # than indexing the list again: the settled task has been dropped.
        assert second.done(), "the released task must have settled"
        assert not first.done(), "the waiter must not wait for every task"
        assert not third.done(), "the waiter must not wait for every task"
        assert pending == [first, third], "only the settled task may be pruned"

        gates[0].set()
        gates[2].set()
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
