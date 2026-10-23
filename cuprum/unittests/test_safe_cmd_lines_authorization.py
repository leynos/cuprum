"""Deferred-authorization contracts for ``SafeCmd.lines()``.

``lines()`` prepares a run; the first ``__anext__`` executes it. Everything
here pins the consequence: the scope in force when the first event is
requested decides whether the program may run and how long it may take, not
the scope that happened to be active when the stream was built.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

import pytest

from cuprum import ScopeConfig, before, observe, scoped
from cuprum.program import Program
from cuprum.sh import ExecutionContext, LineStream
from tests.helpers.catalogue import build_python_catalogue_env
from tests.helpers.timeouts import child_argv, pending_tasks

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.events import ExecEvent
    from cuprum.lines import LineEvent
    from cuprum.sh import CommandResult, SafeCmd
    from cuprum.unittests._safe_cmd_lines_authorization_support import (
        _SpawnRecorder,
    )


@pytest.mark.parametrize(
    "denial",
    [
        pytest.param("deny-all", id="deny-all"),
        pytest.param("other-program", id="narrowed-to-another-program"),
    ],
)
def test_lines_denied_by_the_consuming_scope_never_spawns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    spawn_recorder: _SpawnRecorder,
    denial: str,
) -> None:
    """A stream prepared before a denial is refused when it is finally pulled.

    The stream is built in a permissive scope and its first event is requested
    in a restricted one, spelled either as an empty deny-all allowlist or as an
    allowlist naming a different program. Both must refuse.

    Beyond the ticket's demand, the denial is pinned to happen before anything
    observable exists: no spawn, no before hook, no plan or terminal event, no
    result, no marker file, and no task left on the loop. A later refactor that
    moved the check behind the spawn would still raise, so these assertions are
    what keep the ordering honest.
    """
    from cuprum.context import ForbiddenProgramError

    runtime = build_python_catalogue_env()
    command = runtime.builder(*child_argv(tmp_path / "child-ready"))
    before_calls: list[SafeCmd] = []
    observed: list[ExecEvent] = []
    restricted = (
        ScopeConfig(allowlist=frozenset())
        if denial == "deny-all"
        else ScopeConfig(allowlist=frozenset([Program(str(tmp_path / "other"))]))
    )

    async def consume() -> tuple[CommandResult | None, set[asyncio.Task[object]]]:
        """Build in a permissive scope, pull in a restricted one, then audit."""
        stream = command.lines()
        with (
            scoped(restricted),
            pytest.raises(ForbiddenProgramError),
        ):
            async for _event in stream:
                pass
        return stream.result, pending_tasks()

    with (
        scoped(ScopeConfig(allowlist=frozenset([runtime.program]))),
        before(before_calls.append),
        observe(observed.append),
    ):
        result, stranded = asyncio.run(consume())

    assert result is None, f"a denied stream must not publish a result, got {result!r}"
    assert stranded == set(), (
        f"a denied stream must not leave tasks on the event loop, got {stranded!r}"
    )
    assert not spawn_recorder.executions, (
        "a denied stream must never reach the spawn seam, got "
        f"{spawn_recorder.executions!r}"
    )
    assert not before_calls, "a denied stream must not run before hooks"
    assert not observed, (
        f"a denied stream must not emit plan or terminal events, got {observed!r}"
    )
    assert not (tmp_path / "child-ready").exists(), (
        "a denied stream must not start a child that writes its marker"
    )


def test_lines_denied_after_handoff_between_tasks_never_spawns(
    tmp_path: Path,
    spawn_recorder: _SpawnRecorder,
) -> None:
    """The pulling task's context decides, not the task that built the stream.

    Construction happens in one task; the first ``__anext__`` happens in
    another that iterates inside a deny-all scope, and that scope is what the
    denial is read from. The creating task enters ``async with`` first, so the
    assertion also proves that ``__aenter__()`` does not start the generator:
    entering a stream is not executing it, and the hand-off still governs.
    """
    from cuprum.context import ForbiddenProgramError

    runtime = build_python_catalogue_env()
    command = runtime.builder(*child_argv(tmp_path / "child-ready"))

    async def orchestrate() -> None:
        """Hand a context-managed stream to a task that pulls it under denial."""
        stream = command.lines()
        async with stream:
            consumer = asyncio.create_task(_pull_under_denial(stream))
            with pytest.raises(ForbiddenProgramError):
                await consumer
        assert stream.result is None, (
            "a denied stream must not publish a result after being closed"
        )

    async def _pull_under_denial(stream: LineStream) -> None:
        """Request the first event inside a scope that forbids the program."""
        with scoped(ScopeConfig(allowlist=frozenset())):
            async for _event in stream:
                pass

    with scoped(ScopeConfig(allowlist=frozenset([runtime.program]))):
        asyncio.run(orchestrate())

    assert not spawn_recorder.executions, (
        "the hand-off task's denial must stop the spawn, got "
        f"{spawn_recorder.executions!r}"
    )
    assert not (tmp_path / "child-ready").exists(), (
        "no child may start once the pulling task forbids it"
    )


def test_lines_allowed_by_the_consuming_scope_runs_once(
    python_builder: cabc.Callable[..., SafeCmd],
    spawn_recorder: _SpawnRecorder,
) -> None:
    """A stream whose consuming scope permits the program runs exactly once."""
    command = python_builder("-c", "print('allowed')")

    async def collect() -> tuple[list[LineEvent], CommandResult | None]:
        """Build and consume inside the same permissive scope."""
        stream = command.lines()
        events = [event async for event in stream]
        return events, stream.result

    with scoped(ScopeConfig(allowlist=frozenset([command.program]))):
        events, result = asyncio.run(collect())

    assert [event.text for event in events] == ["allowed"], (
        f"an allowed stream must yield its lines, got {events!r}"
    )
    assert result is not None, "an allowed stream must publish its result"
    assert result.ok, f"an allowed stream must succeed, got {result!r}"
    assert len(spawn_recorder.executions) == 1, (
        "deferred authorization must spawn exactly once, got "
        f"{len(spawn_recorder.executions)} spawns"
    )
    assert spawn_recorder.executions[0].cmd.program == command.program, (
        "the recorded execution must be the one the stream prepared"
    )


@dc.dataclass(frozen=True, slots=True)
class _DeadlineCase:
    """One row of the deadline-precedence table.

    The three values travel together because they only mean anything as a set:
    a row is "these two inputs must resolve to this deadline". Keeping them in
    one object also keeps the test signature about the fixtures it needs rather
    than about how many columns the table happens to have.
    """

    explicit: float | None
    context_timeout: float | None
    expected: float


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _DeadlineCase(None, None, 30.0),
            id="explicit-none-falls-through",
        ),
        pytest.param(
            _DeadlineCase(7.0, None, 7.0),
            id="explicit-timeout-wins",
        ),
        pytest.param(
            _DeadlineCase(None, 11.0, 11.0),
            id="context-timeout-wins-over-scope",
        ),
        pytest.param(
            _DeadlineCase(7.0, 11.0, 7.0),
            id="explicit-wins-over-context",
        ),
    ],
)
def test_lines_resolve_the_deadline_when_consumed(
    python_builder: cabc.Callable[..., SafeCmd],
    spawn_recorder: _SpawnRecorder,
    case: _DeadlineCase,
) -> None:
    """The timeout is re-resolved for the consuming scope, not the building one.

    Every row builds the stream inside a scope whose timeout is ``2.0``, then
    pulls it inside one whose timeout is ``30.0``, and reads the timeout the
    spawn seam actually received. That ``2.0`` is the stream's construction-time
    default and never the answer: a scope's timeout must not follow a prepared
    stream into a scope the caller has since entered.

    The first row passes ``None`` explicitly, which is the same call as
    omitting the argument, and confirms ``None`` inherits the fallbacks rather
    than disabling timeouts. The remaining rows pin the precedence: an explicit
    value beats ``ExecutionContext.timeout``, which beats the consuming scope.
    """
    command = python_builder("-c", "pass")
    context = ExecutionContext(timeout=case.context_timeout)

    async def collect() -> None:
        """Build in one scope, then consume in another."""
        stream = command.lines(timeout=case.explicit, context=context)
        async with stream:
            with scoped(ScopeConfig(timeout=30.0)):
                async for _event in stream:
                    pass

    with scoped(ScopeConfig(allowlist=frozenset([command.program]), timeout=2.0)):
        asyncio.run(collect())

    assert len(spawn_recorder.executions) == 1, (
        "an allowed run must reach the spawn seam once, got "
        f"{len(spawn_recorder.executions)}"
    )
    resolved = spawn_recorder.executions[0].timeout
    assert resolved == case.expected, (
        f"the spawn must receive the resolved deadline {case.expected!r}, "
        f"got {resolved!r}"
    )


@pytest.mark.parametrize(
    "requested",
    [
        pytest.param(0.0, id="explicit-zero-retained"),
        pytest.param(None, id="inherited-zero-from-scope"),
    ],
)
def test_lines_zero_timeout_expires_immediately(
    tmp_path: Path,
    spawn_recorder: _SpawnRecorder,
    *,
    requested: float | None,
) -> None:
    """A zero deadline expires at once, whether explicit or inherited.

    Zero is a value, not a synonym for "no timeout", so it must survive
    resolution rather than fall through as if unset. The inherited row is
    built in a scope whose timeout is ``2.0`` and consumed in one carrying
    ``0.0``, which is the stale-preparation bug in its sharpest form: the
    prepared record held a live deadline and the consuming scope has already
    elapsed.

    The child in ``child_argv`` blocks for five minutes, so a run that
    returns at all can only have honoured the deadline.
    """
    from cuprum.sh import TimeoutExpired

    runtime = build_python_catalogue_env()
    command = runtime.builder(*child_argv(tmp_path / "child-ready"))

    async def consume() -> None:
        """Build and consume, expecting the deadline to expire at once."""
        stream = command.lines(timeout=requested)
        with (
            scoped(ScopeConfig(timeout=0.0)),
            pytest.raises(TimeoutExpired) as expiry,
        ):
            async for _event in stream:
                pass
        # The zero must survive resolution untouched, not merely round to it.
        assert expiry.value.timeout == 0.0, (  # ruff: ignore[float-equality-comparison] - exact sentinel retention
            f"the report must carry the resolved zero deadline, got {expiry.value!r}"
        )

    with scoped(ScopeConfig(allowlist=frozenset([runtime.program]), timeout=2.0)):
        asyncio.run(consume())

    assert len(spawn_recorder.executions) == 1, (
        "the child must have been spawned before its deadline expired"
    )
    assert spawn_recorder.executions[0].timeout == 0.0, (  # ruff: ignore[float-equality-comparison] - exact sentinel retention
        "a zero deadline must reach the spawn unchanged, got "
        f"{spawn_recorder.executions[0].timeout!r}"
    )
