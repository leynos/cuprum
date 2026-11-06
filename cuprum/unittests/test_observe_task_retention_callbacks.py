"""Composed line callbacks under the observer-task retention ceiling.

The composed callback is the seam where admission meets dispatch: below the
ceiling it emits inline, and at the ceiling it returns a coroutine that waits
for a slot. These tests pin that behaviour, the decode-time stamp a deferred
line keeps, the per-event overshoot of a multi-hook event, and the ordering
guarantee that a failed observation never reaches the caller's ``on_line``.

The pruning and admission rules themselves live in
``test_observe_task_retention.py``; the telemetry in
``test_observe_task_retention_telemetry.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import typing as typ

import pytest

from cuprum._line_callbacks import (
    _compose_line_callbacks,
    _LineEmissionContext,
)
from cuprum._observability import _wait_for_exec_hook_tasks
from cuprum.unittests.test_line_event_emission_support import _make_observation
from cuprum.unittests.test_observe_task_retention import _HookFailureError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent
    from cuprum.lines import LineEvent


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
    monkeypatch.setattr("cuprum._line_event_emitter._OBSERVE_TASK_CEILING", 2)

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
    monkeypatch.setattr("cuprum._line_event_emitter._OBSERVE_TASK_CEILING", 1)

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
    monkeypatch.setattr("cuprum._line_event_emitter._OBSERVE_TASK_CEILING", 3)

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
