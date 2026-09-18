"""Unit tests for LineEvent and the composed per-line callback."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import dataclasses as dc
import inspect
import typing as typ

import pytest

from cuprum import _line_callbacks, lines
from cuprum._line_callbacks import (
    _compose_line_callbacks,
    _fan_out_hooks,
    _LineEmissionContext,
)
from cuprum._pipeline_types import _ExecutionHooks, _StageObservation

type _LineConsumer = cabc.Callable[[str], None]


async def _await_hook_outcome(outcome: cabc.Awaitable[None]) -> None:
    """Await a line-hook outcome accepted by the public callback type."""
    await outcome


if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent
    from cuprum.lines import LineEvent
    from cuprum.sh import SafeCmd


def test_line_event_is_frozen() -> None:
    """LineEvent rejects mutation so observers cannot rewrite history."""
    event = lines.LineEvent(stream="stdout", at=1.5, text="hello")

    with pytest.raises(dc.FrozenInstanceError):
        event.text = "mutated"  # ty: ignore[invalid-assignment] - deliberate mutation attempt


def test_compose_returns_none_without_observers() -> None:
    """The zero-observer path stays callback-free."""
    callback = _compose_line_callbacks(
        _make_observation(),
        _LineEmissionContext(stream="stdout", pid=1, on_line=None, started_at=0.0),
    )

    assert callback is None, (
        "no observe hooks and no on_line must keep the no-callback path"
    )


def test_compose_stamps_elapsed_monotonic_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The at field is the pinned clock reading minus the start reference."""
    reading = {"value": 10.0}
    monkeypatch.setattr(_line_callbacks, "perf_counter", lambda: reading["value"])
    received: list[LineEvent] = []
    callback = _compose_line_callbacks(
        _make_observation(),
        _LineEmissionContext(
            stream="stderr",
            pid=7,
            on_line=received.append,
            started_at=9.25,
        ),
    )

    reading["value"] = 12.5
    typ.cast("_LineConsumer", callback)("a line")

    assert len(received) == 1, f"one line must yield one event, got {received!r}"
    event = received[0]
    assert event.stream == "stderr", (
        f"the emission context must tag the stream, got {event!r}"
    )
    assert event.at == pytest.approx(3.25), (
        f"the stamp must be the relative monotonic reading, got {event!r}"
    )
    assert event.text == "a line", (
        f"the event must retain the decoded line text, got {event!r}"
    )


def test_compose_fans_out_to_user_callback_with_stream_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each composed callback stamps its own stream name."""
    monkeypatch.setattr(_line_callbacks, "perf_counter", lambda: 5.0)
    received: list[LineEvent] = []
    callback = _compose_line_callbacks(
        _make_observation(),
        _LineEmissionContext(
            stream="stdout",
            pid=42,
            on_line=received.append,
            started_at=4.0,
        ),
    )

    typ.cast("_LineConsumer", callback)("tagged")

    assert [event.stream for event in received] == ["stdout"], (
        f"each composed callback must stamp its own stream, got {received!r}"
    )
    assert [event.text for event in received] == ["tagged"], (
        f"the user callback must receive the decoded line, got {received!r}"
    )
    assert received[0].at == pytest.approx(1.0), (
        f"the stamp must be measured from the start reference, got {received!r}"
    )


def test_compose_fans_out_to_observe_output_event() -> None:
    """The unit composition path retains the stream and decoded line payload."""
    observed: list[ExecEvent] = []
    callback = _compose_line_callbacks(
        _make_observation((observed.append,)),
        _LineEmissionContext(stream="stdout", pid=42, on_line=None, started_at=0.0),
    )

    typ.cast("_LineConsumer", callback)("observed")

    assert [(event.phase, event.pid, event.line) for event in observed] == [
        ("stdout", 42, "observed"),
    ], f"observe output event must retain decoded stream data, got {observed!r}"


def test_fan_out_hooks_runs_synchronous_hooks_in_order() -> None:
    """Synchronous fan-out preserves registration order without an awaitable."""
    received: list[str] = []

    def first(_event: LineEvent) -> None:
        """Record the first synchronous callback."""
        received.append("first")

    def second(_event: LineEvent) -> None:
        """Record the second synchronous callback."""
        received.append("second")

    callback = _fan_out_hooks((first, second))

    assert callback(lines.LineEvent(stream="stdout", at=0.0, text="line")) is None, (
        "synchronous hooks must not return an awaitable outcome"
    )
    assert received == ["first", "second"], (
        f"synchronous hooks must preserve registration order, got {received!r}"
    )


def test_fan_out_hooks_awaits_async_outcomes_in_order() -> None:
    """Awaited fan-out outcomes retain registration order."""
    received: list[str] = []

    async def first(_event: LineEvent) -> None:
        """Record the first awaited callback."""
        received.append("first")
        await asyncio.sleep(0)

    async def second(_event: LineEvent) -> None:
        """Record the second awaited callback."""
        received.append("second")
        await asyncio.sleep(0)

    callback = _fan_out_hooks((first, second))
    outcome = callback(lines.LineEvent(stream="stdout", at=0.0, text="line"))

    assert outcome is not None, "async fan-out must return an awaitable"
    asyncio.run(_await_hook_outcome(outcome))
    assert received == ["first", "second"], (
        f"async outcomes must be awaited in registration order, got {received!r}"
    )


def test_fan_out_hooks_invokes_every_hook_before_awaiting() -> None:
    """Every hook produces its outcome before the first outcome is awaited."""
    trace: list[str] = []

    async def wait_for_outcome(name: str) -> None:
        """Record one outcome beginning execution."""
        trace.append(f"await {name}")
        await asyncio.sleep(0)

    def first(_event: LineEvent) -> cabc.Awaitable[None]:
        """Return the first deferred outcome."""
        trace.append("hook first")
        return wait_for_outcome("first")

    def second(_event: LineEvent) -> cabc.Awaitable[None]:
        """Return the second deferred outcome."""
        trace.append("hook second")
        return wait_for_outcome("second")

    callback = _fan_out_hooks((first, second))
    outcome = callback(lines.LineEvent(stream="stdout", at=0.0, text="line"))

    assert trace == ["hook first", "hook second"], (
        f"all hooks must run before awaiting begins, got {trace!r}"
    )
    assert outcome is not None, "deferred outcomes require a combined awaitable"
    asyncio.run(_await_hook_outcome(outcome))
    assert trace == ["hook first", "hook second", "await first", "await second"], (
        f"outcomes must await in registration order, got {trace!r}"
    )


def test_fan_out_hooks_closes_skipped_coroutine_after_failure() -> None:
    """An earlier async failure closes an unawaited later coroutine outcome."""
    expected = ValueError("first callback failed")
    later_outcome: object | None = None

    async def fail(_event: LineEvent) -> None:
        """Raise the original callback failure."""
        await asyncio.sleep(0)
        raise expected

    async def later(_event: LineEvent) -> None:
        """Stand in for a coroutine that must be closed without execution."""
        await asyncio.sleep(0)

    def later_hook(event: LineEvent) -> cabc.Awaitable[None]:
        """Retain the later coroutine so its closed state can be asserted."""
        nonlocal later_outcome
        later_outcome = later(event)
        return typ.cast("cabc.Awaitable[None]", later_outcome)

    callback = _fan_out_hooks((fail, later_hook))
    outcome = callback(lines.LineEvent(stream="stdout", at=0.0, text="line"))

    assert outcome is not None, "async fan-out must return an awaitable"
    with pytest.raises(ValueError, match="first callback failed") as caught:
        asyncio.run(_await_hook_outcome(outcome))
    assert caught.value is expected, "fan-out must preserve the original failure"
    assert inspect.iscoroutine(later_outcome), "the later hook must return a coroutine"
    assert later_outcome.cr_frame is None, (
        "a skipped later coroutine must be closed after an earlier failure"
    )


class _NullCmd:
    """Command stand-in for the observation."""

    program: object = None
    argv: tuple[str, ...] = ()

    @property
    def argv_with_program(self) -> tuple[str, ...]:
        """The argv without a program name."""
        return self.argv

    @property
    def project(self) -> object:
        """The project name stand-in."""
        return _NullProject()


class _NullProject:
    """Project stand-in carrying a name."""

    name = "test-project"


def _make_observation(
    observe_hooks: tuple[cabc.Callable[[ExecEvent], None], ...] = (),
) -> _StageObservation:
    """Build a minimal observation with the requested observe hooks."""
    hooks = _ExecutionHooks(
        before_hooks=(),
        after_hooks=(),
        observe_hooks=observe_hooks,
    )
    return _StageObservation(
        cmd=typ.cast("SafeCmd", _NullCmd()),
        hooks=hooks,
        tags={},
        cwd=None,
        env_overlay=None,
        pending_tasks=[],
        wall_clock=lambda: 0.0,
    )
