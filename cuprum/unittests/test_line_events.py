"""Unit tests for LineEvent and the composed per-line callback."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import typing as typ

import pytest

from cuprum import _line_callbacks, lines
from cuprum._line_callbacks import _compose_line_callbacks, _LineEmissionContext
from cuprum._pipeline_types import _ExecutionHooks, _StageObservation

type _LineConsumer = cabc.Callable[[str], None]

if typ.TYPE_CHECKING:
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

    assert len(received) == 1
    event = received[0]
    assert event.stream == "stderr"
    assert event.at == pytest.approx(3.25)
    assert event.text == "a line"


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

    assert [event.stream for event in received] == ["stdout"]
    assert [event.text for event in received] == ["tagged"]
    assert received[0].at == pytest.approx(1.0)


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


def _make_observation() -> _StageObservation:
    """Build a minimal observation with no observe hooks."""
    hooks = _ExecutionHooks(before_hooks=(), after_hooks=(), observe_hooks=())
    return _StageObservation(
        cmd=typ.cast("SafeCmd", _NullCmd()),
        hooks=hooks,
        tags={},
        cwd=None,
        env_overlay=None,
        pending_tasks=[],
        wall_clock=lambda: 0.0,
    )
