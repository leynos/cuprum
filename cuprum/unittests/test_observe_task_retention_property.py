"""Retention properties: what pruning must preserve, and what admission bounds.

Finalization reports the first failed or cancelled task in list order. Pruning
is only safe if removing settled tasks never changes which task that is — not
across a single prune, and not across several prune steps interleaved with new
spawns and with earlier tasks that only settle later. The failure properties
compare the pruned outcome against the unpruned one for the same sequence, so
the prune is held to exactly that contract rather than to a hand-picked
example. The single-step rules themselves are tested in
``test_observe_task_retention.py``.

The last property covers the documented admission bound instead: with two
streams admitting into one shared list, and with settled tasks appearing
between admissions in arbitrary combinations, the unfinished count must stay at
or below ``ceiling + hooks_per_event - 1``, and every line must still reach
every hook exactly once, in order.
"""

from __future__ import annotations

import asyncio
import inspect
import typing as typ
from unittest import mock

from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum._line_callbacks import _compose_line_callbacks, _LineEmissionContext
from cuprum._observability import _wait_for_exec_hook_tasks
from cuprum._observe_task_retention import _prune_finished_hook_tasks
from cuprum.unittests.test_line_event_emission_support import _make_observation
from cuprum.unittests.test_observe_task_retention import (
    _HookFailureError,
    _spawn,
)
from cuprum.unittests.test_observe_task_retention_callbacks import _RecordingHook

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    _Outcome = tuple[str, str | None] | None
    _SequenceSelector = cabc.Callable[..., cabc.Coroutine[object, object, _Outcome]]


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


def _assert_pruning_preserves_outcome(
    select: _SequenceSelector,
    sequence: list[str],
    *,
    case: str,
) -> None:
    """Assert pruning selects exactly the outcome the unpruned drain selects."""
    unpruned = asyncio.run(select(sequence, prune=False))
    pruned = asyncio.run(select(sequence, prune=True))

    assert pruned == unpruned, (
        f"{case} changed the reported outcome for {sequence!r}: "
        f"{pruned!r} != {unpruned!r}"
    )


@settings(max_examples=40, deadline=None)
@given(spec=st.lists(st.sampled_from(("ok", "fail", "cancel", "pending")), max_size=6))
def test_pruning_preserves_the_failure_finalization_reports(
    spec: list[str],
) -> None:
    """Pruning must select exactly the failure the unpruned drain selects."""
    _assert_pruning_preserves_outcome(_selected_failure, spec, case="pruning")


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

    Returns
    -------
    tuple[str, str | None] | None
        The kind and marker of the failure finalization reports, or ``None``
        when the drain raised nothing.
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
    _assert_pruning_preserves_outcome(
        _selected_failure_multistep,
        steps,
        case="repeated pruning",
    )


_Stream = typ.Literal["stdout", "stderr"]
_STREAMS: tuple[_Stream, _Stream] = ("stdout", "stderr")


def _stream_context(stream: _Stream) -> _LineEmissionContext:
    """Build one stream's emission context, with no caller ``on_line``."""
    return _LineEmissionContext(
        stream=stream,
        pid=1,
        on_line=None,
        started_at=0.0,
        monotonic_clock=lambda: 1.0,
    )


def _unfinished(tasks: list[asyncio.Task[None]]) -> int:
    """Count the retained tasks that have not settled yet."""
    return sum(not task.done() for task in tasks)


async def _settle_selected(hooks: list[_RecordingHook], mask: int) -> None:
    """Let the hooks a bit of ``mask`` selects settle, then hold them again."""
    for index, hook in enumerate(hooks):
        if mask & (1 << index):
            hook.gate.set()
    for _ in range(2):
        await asyncio.sleep(0)
    for hook in hooks:
        hook.gate.clear()


@settings(max_examples=40, deadline=None)
@given(
    ceiling=st.integers(min_value=1, max_value=5),
    hook_count=st.integers(min_value=1, max_value=3),
    steps=st.lists(
        st.tuples(st.integers(min_value=0, max_value=1), st.integers(0, 7)),
        min_size=1,
        max_size=10,
    ),
)
def test_unfinished_tasks_stay_within_the_admission_bound(
    ceiling: int,
    hook_count: int,
    steps: list[tuple[int, int]],
) -> None:
    """Two streams admitting into one list must never exceed the bound."""
    bound = ceiling + hook_count - 1

    async def case() -> None:
        """Emit every generated line, settling a chosen subset between them."""
        hooks = [_RecordingHook(asyncio.Event()) for _ in range(hook_count)]
        observation = _make_observation(tuple(hooks))
        emitters = [
            _compose_line_callbacks(observation, _stream_context(stream))
            for stream in _STREAMS
        ]
        emitted: list[str] = []
        for index, (producer, settle_mask) in enumerate(steps):
            emit = emitters[producer]
            assert emit is not None, "an observing stream must compose a callback"
            line = f"p{producer}-{index}"
            emitted.append(line)
            outcome = emit(line)
            if inspect.isawaitable(outcome):
                for hook in hooks:
                    hook.gate.set()
                await outcome
                for hook in hooks:
                    hook.gate.clear()
            await _settle_selected(hooks, settle_mask)
            unfinished = _unfinished(observation.pending_tasks)
            assert unfinished <= bound, (
                f"{unfinished} unfinished tasks exceed ceiling {ceiling} plus "
                f"{hook_count} hooks minus one after line {index}"
            )

        for hook in hooks:
            hook.gate.set()
        await _wait_for_exec_hook_tasks(observation.pending_tasks)
        for position, hook in enumerate(hooks):
            assert hook.seen == emitted, (
                f"hook {position} must see every line exactly once, in order"
            )

    with mock.patch("cuprum._line_event_emitter._OBSERVE_TASK_CEILING", ceiling):
        asyncio.run(case())
