"""Runtime coverage for CQRS hook and task-scheduling behaviour."""

from __future__ import annotations

import asyncio
import itertools
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum import (
    LS,
    ForbiddenProgramError,
    ScopeConfig,
    before,
    scoped,
    sh,
)
from cuprum._observability import (
    _emit_exec_event,
    _ExecEventEmissionError,
    _wait_for_exec_hook_tasks,
)
from cuprum._pipeline_types import _EventDetails, _ExecutionHooks, _StageObservation
from cuprum.unittests._cqrs_fixtures import _echo_cmd, _event

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent


class _GeneratedHookError(Exception):
    """Test exception raised by generated hook-ordering cases."""


def test_safe_cmd_run_enforces_allowlist_before_before_hooks() -> None:
    """A forbidden ``SafeCmd.run_sync`` must not dispatch before hooks."""
    calls: list[sh.SafeCmd] = []

    def track_before(cmd: sh.SafeCmd) -> None:
        """Record whether a before hook ran."""
        calls.append(cmd)

    with (
        scoped(ScopeConfig(allowlist=frozenset([LS]))),
        before(track_before),
        pytest.raises(ForbiddenProgramError),
    ):
        _echo_cmd().run_sync()

    assert calls == [], "before hooks should not run before allowlist enforcement"


@settings(deadline=None)
@given(
    hook_kinds=st.lists(
        st.sampled_from(("sync", "async", "fail")),
        min_size=1,
        max_size=8,
    )
)
def test_emit_exec_event_preserves_scheduled_prefix_for_generated_hook_orders(
    hook_kinds: list[str],
) -> None:
    """Generated hook orderings preserve tasks scheduled before failure."""

    async def run() -> None:
        """Drive generated hooks inside a running loop."""
        hooks = tuple(_generated_observe_hook(kind) for kind in hook_kinds)
        first_failure_index = _first_failure_index(hook_kinds)
        expected_scheduled = hook_kinds[
            : first_failure_index if first_failure_index is not None else None
        ].count("async")

        if first_failure_index is None:
            scheduled = _emit_exec_event(hooks, _event())
            assert len(scheduled) == expected_scheduled, (
                "all generated async hooks should be scheduled when no hook fails"
            )
            await asyncio.gather(*scheduled)
            return

        with pytest.raises(_ExecEventEmissionError) as exc_info:
            _emit_exec_event(hooks, _event())
        assert len(exc_info.value.scheduled_tasks) == expected_scheduled, (
            "failing hook emission should carry tasks scheduled before failure"
        )
        await asyncio.gather(*exc_info.value.scheduled_tasks)

    asyncio.run(run())


def _first_failure_index(hook_kinds: list[str]) -> int | None:
    """Return the index of the first generated failing hook."""
    try:
        return hook_kinds.index("fail")
    except ValueError:
        return None


def _generated_observe_hook(
    kind: str,
) -> cabc.Callable[[ExecEvent], cabc.Awaitable[None] | None]:
    """Build a generated observe hook of the requested kind."""

    async def async_hook(_event: ExecEvent) -> None:
        """Cross an async boundary for generated hook scheduling."""
        await asyncio.sleep(0)

    def sync_hook(event: ExecEvent) -> None:
        """Run a generated synchronous hook."""
        _ = event.phase

    def fail_hook(_event: ExecEvent) -> None:
        """Raise from a generated hook."""
        raise _GeneratedHookError

    match kind:
        case "async":
            return async_hook
        case "sync":
            return sync_hook
        case "fail":
            return fail_hook
        case _:
            msg = "unknown generated hook kind"
            raise ValueError(msg)


def test_stage_observations_share_pending_tasks_under_concurrent_emits() -> None:
    """Concurrent stage emissions append to the shared pending-task list."""

    async def run() -> None:
        """Emit from multiple stage observations in one event loop."""
        pending_tasks: list[asyncio.Task[None]] = []
        completed: list[int] = []
        observations = _stage_observations(pending_tasks, completed)

        await asyncio.gather(
            *itertools.starmap(_emit_after_yield, enumerate(observations))
        )

        assert len(pending_tasks) == len(observations), (
            "each concurrent stage emission should append one pending task"
        )
        await _wait_for_exec_hook_tasks(pending_tasks)
        assert sorted(completed) == list(range(len(observations))), (
            "all scheduled stage observe tasks should complete exactly once"
        )

    asyncio.run(run())


def _stage_observations(
    pending_tasks: list[asyncio.Task[None]],
    completed: list[int],
) -> tuple[_StageObservation, ...]:
    """Build observations sharing one pending-task collection."""

    async def async_hook(event: ExecEvent) -> None:
        """Record the stage index after an async scheduling boundary."""
        await asyncio.sleep(0)
        completed.append(typ.cast("int", event.tags["pipeline_stage_index"]))

    return tuple(
        _StageObservation(
            cmd=_echo_cmd(),
            hooks=_ExecutionHooks(
                before_hooks=(),
                after_hooks=(),
                observe_hooks=(async_hook,),
            ),
            tags={"pipeline_stage_index": idx},
            cwd=None,
            env_overlay=None,
            pending_tasks=pending_tasks,
        )
        for idx in range(4)
    )


async def _emit_after_yield(idx: int, observation: _StageObservation) -> None:
    """Yield once, then emit on the active event loop."""
    await asyncio.sleep(0)
    observation.emit("start", _EventDetails(pid=idx))
