"""Failure selection under pruning, as a property.

Finalization reports the first failed or cancelled task in list order. Pruning
is only safe if removing settled tasks never changes which task that is — not
across a single prune, and not across several prune steps interleaved with new
spawns and with earlier tasks that only settle later. Both properties compare
the pruned outcome against the unpruned one for the same sequence, so the prune
is held to exactly that contract rather than to a hand-picked example. The
single-step rules themselves are tested in
``test_observe_task_retention.py``.
"""

from __future__ import annotations

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum._observability import _wait_for_exec_hook_tasks
from cuprum._observe_task_retention import _prune_finished_hook_tasks
from cuprum.unittests.test_observe_task_retention import (
    _HookFailureError,
    _spawn,
)


def _describe(exc: BaseException) -> tuple[str, str | None]:
    """Render the part of a failure that finalization is expected to report."""
    if isinstance(exc, _HookFailureError):
        return ("failure", exc.marker)
    if isinstance(exc, asyncio.CancelledError):
        return ("cancelled", None)
    return (type(exc).__name__, str(exc))


async def _drain_outcome(
    tasks: list[asyncio.Task[None]],
) -> tuple[str, str | None] | None:
    """Release every held task and return the outcome finalization reports."""
    try:
        await _wait_for_exec_hook_tasks(tasks)
    except BaseException as exc:  # ruff: ignore[blind-except] - selecting is the test
        return _describe(exc)
    return None


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
    return await _drain_outcome(tasks)


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


_PRUNE = "prune"
_LATE_CANCEL = "late-cancel"
_SPAWN_KINDS = st.sampled_from((
    "ok",
    "fail",
    "cancel",
    "pending",
    "late-fail",
    _LATE_CANCEL,
))
_SPAN = st.lists(_SPAWN_KINDS, max_size=4)


def _interleave(before: list[str], middle: list[str], after: list[str]) -> list[str]:
    """Place two prune steps between three spans of spawn steps."""
    return [*before, _PRUNE, *middle, _PRUNE, *after]


async def _selected_failure_multistep(
    steps: list[str], *, prune: bool
) -> tuple[str, str | None] | None:
    """Return the outcome finalization reports for a multi-round sequence.

    Tasks spawned before a prune step settle before it runs, so each prune sees
    a mix of settled successes, settled failures, and tasks that stay
    unfinished until the final gate. ``late-fail`` and ``late-cancel`` tasks
    are the ones that outlive every prune and only then fail or are cancelled.
    """
    gate = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    late_cancelled: list[asyncio.Task[None]] = []
    for index, step in enumerate(steps):
        if step == _PRUNE:
            await asyncio.sleep(0)
            if prune:
                _prune_finished_hook_tasks(tasks)
            continue
        task = _spawn(step, gate, marker=f"failure-{index}")
        tasks.append(task)
        if step == _LATE_CANCEL:
            late_cancelled.append(task)
    for task in late_cancelled:
        task.cancel()
    gate.set()
    return await _drain_outcome(tasks)


@settings(max_examples=60, deadline=None)
@given(steps=st.builds(_interleave, _SPAN, _SPAN, _SPAN))
def test_repeated_pruning_preserves_the_failure_finalization_reports(
    steps: list[str],
) -> None:
    """Every prune step must leave the outcome an unpruned drain would report."""
    unpruned = asyncio.run(_selected_failure_multistep(steps, prune=False))
    pruned = asyncio.run(_selected_failure_multistep(steps, prune=True))

    assert pruned == unpruned, (
        f"repeated pruning changed the reported outcome for {steps!r}: "
        f"{pruned!r} != {unpruned!r}"
    )
