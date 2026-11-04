"""Behavioural tests for observe-hook dispatch across hook shapes.

Every observe hook receives every line event, whatever shape it is written in,
and every awaitable a hook returns is awaited before the synchronous run
returns. The scenario drives one real subprocess per example row so the
per-line dispatch path is exercised, not simulated.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import functools
import gc
import typing as typ
import warnings

from pytest_bdd import given, parsers, scenario, then, when

from cuprum import ScopeConfig, scoped, sh
from tests.behaviour._structured_events_support import (
    catalogue_and_builder,
    join_script,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import contextvars

    import pytest

    from cuprum.catalogue import ProgramCatalogue
    from cuprum.events import ExecEvent, ExecHook
    from cuprum.sh import SafeCmd


@scenario(
    "../features/observe_hook_dispatch.feature",
    "Every hook shape receives every line and settles before the run returns",
)
def test_every_hook_shape_receives_every_line_and_settles() -> None:
    """Behavioural coverage for observe-hook dispatch across hook shapes."""


@dc.dataclass(frozen=True, slots=True)
class _Command:
    """The scenario's allowlist and the command built against it."""

    catalogue: ProgramCatalogue
    cmd: SafeCmd


@dc.dataclass(slots=True)
class _DispatchState:
    """What one example row observed while the command ran."""

    shape: str
    hook: ExecHook | None = None
    recorded: list[ExecEvent] = dc.field(default_factory=list)
    tasks: list[asyncio.Task[object]] = dc.field(default_factory=list)
    caught_warnings: list[warnings.WarningMessage] = dc.field(default_factory=list)


def _record_stdout(recorded: list[ExecEvent], event: ExecEvent) -> None:
    """Retain a stdout line event, so ordering assertions see only lines."""
    if event.phase == "stdout":
        recorded.append(event)


def _sync_hook(recorded: list[ExecEvent]) -> ExecHook:
    """Return a synchronous hook."""

    def hook(event: ExecEvent) -> None:
        """Record a stdout line event synchronously."""
        _record_stdout(recorded, event)

    return hook


def _async_def_hook(recorded: list[ExecEvent]) -> ExecHook:
    """Return an ``async def`` hook."""

    async def hook(event: ExecEvent) -> None:  # ruff: ignore[unused-async] - async shape under test
        """Record a stdout line event from a coroutine."""
        _record_stdout(recorded, event)

    return hook


def _lambda_hook(recorded: list[ExecEvent]) -> ExecHook:
    """Return a lambda that calls a coroutine function."""

    async def body(event: ExecEvent) -> None:  # ruff: ignore[unused-async] - coroutine shape under test
        """Record a stdout line event from the lambda's coroutine."""
        _record_stdout(recorded, event)

    # The lambda *is* the shape under test: a sync callable that returns a
    # coroutine without being a coroutine function.
    return lambda event: body(event)  # ruff: ignore[unnecessary-lambda] - the shape under test


def _wraps_hook(recorded: list[ExecEvent]) -> ExecHook:
    """Return a ``functools.wraps`` synchronous wrapper of ``async def``."""

    async def inner(event: ExecEvent) -> None:  # ruff: ignore[unused-async] - coroutine shape under test
        """Record a stdout line event from the wrapped coroutine."""
        _record_stdout(recorded, event)

    @functools.wraps(inner)
    def hook(event: ExecEvent) -> cabc.Coroutine[None, None, None]:
        """Return the wrapped coroutine for the event."""
        return inner(event)

    return hook


class _AsyncRecorder:
    """An instance whose ``__call__`` is an ``async def``."""

    def __init__(self, recorded: list[ExecEvent]) -> None:
        """Bind the shared line recorder."""
        self._recorded = recorded

    async def __call__(self, event: ExecEvent) -> None:
        """Record a stdout line event."""
        _record_stdout(self._recorded, event)


_SHAPES: dict[str, cabc.Callable[[list[ExecEvent]], ExecHook]] = {
    "a synchronous function": _sync_hook,
    "an async def function": _async_def_hook,
    "a lambda returning a coroutine": _lambda_hook,
    "a functools.wraps wrapper of async def": _wraps_hook,
    "an instance with async __call__": _AsyncRecorder,
}

# Only these shapes can schedule an observe task; the synchronous function
# cannot, and asserting otherwise would make the settled-task check vacuous
# in the opposite direction.
_ASYNC_SHAPES = frozenset(_SHAPES) - {"a synchronous function"}


@given(
    "a safe command that prints 5 numbered lines to stdout",
    target_fixture="dispatch_command",
)
def given_command() -> _Command:
    """Build an echo-free interpreter command printing five numbered lines."""
    catalogue, build = catalogue_and_builder()
    script = join_script(
        "for index in range(1, 6):",
        '    print(f"line-{index}")',
    )
    return _Command(catalogue=catalogue, cmd=build("-c", script))


@given(
    parsers.parse("an observe hook shaped as {shape} that records stdout line events"),
    target_fixture="dispatch_state",
)
def given_hook(shape: str) -> _DispatchState:
    """Build the observe hook named by the example row's ``shape``."""
    state = _DispatchState(shape=shape)
    state.hook = _SHAPES[shape](state.recorded)
    return state


@when("the command runs synchronously with line events observed")
def when_run(
    dispatch_command: _Command,
    dispatch_state: _DispatchState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the command under the hook and record every task it schedules."""
    hook = dispatch_state.hook
    assert hook is not None, "the hook step must run before the run step"
    created: list[asyncio.Task[object]] = []
    real_create_task = asyncio.create_task

    def record(
        coro: cabc.Coroutine[object, object, object],
        *,
        name: str | None = None,
        context: contextvars.Context | None = None,
    ) -> asyncio.Task[object]:
        """Create the task and retain it for the settled-task check."""
        task = real_create_task(coro, name=name, context=context)
        created.append(task)
        return task

    monkeypatch.setattr(asyncio, "create_task", typ.cast("typ.Any", record))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        allowlist = dispatch_command.catalogue.allowlist
        with scoped(ScopeConfig(allowlist=allowlist)), sh.observe(hook):
            _ = dispatch_command.cmd.run_sync()
        gc.collect()
    dispatch_state.tasks = created
    dispatch_state.caught_warnings = caught


@then("the hook records exactly 5 stdout line events in print order")
def then_records_lines(dispatch_state: _DispatchState) -> None:
    """Every line reaches the hook once, in print order."""
    lines = [event.line for event in dispatch_state.recorded]
    assert lines == [f"line-{index}" for index in range(1, 6)], (
        f"the hook recorded {lines!r} instead of the five printed lines in order"
    )


@then("no observe-hook task is pending when the run returns")
def then_tasks_settled(dispatch_state: _DispatchState) -> None:
    """Every task the hook caused is complete before ``run_sync`` returns."""
    observe_tasks = [
        task
        for task in dispatch_state.tasks
        if task.get_name().startswith("cuprum.observe.")
    ]
    if dispatch_state.shape in _ASYNC_SHAPES:
        assert observe_tasks, "an awaitable result must be scheduled"
    pending = [task for task in observe_tasks if not task.done()]
    assert not pending, f"{len(pending)} observe-hook tasks were still pending"


@then("no coroutine-never-awaited warning was emitted")
def then_no_never_awaited(dispatch_state: _DispatchState) -> None:
    """No hook result was dropped instead of scheduled."""
    never_awaited = [
        warning
        for warning in dispatch_state.caught_warnings
        if "never awaited" in str(warning.message)
    ]
    assert not never_awaited, [str(warning.message) for warning in never_awaited]
