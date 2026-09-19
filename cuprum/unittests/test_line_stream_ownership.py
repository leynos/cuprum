"""Post-spawn ownership on the ``SafeCmd.lines()`` path.

``_start_line_stream_run`` spawns the child before it can hand ownership back,
and the ``start`` event it emits in between runs synchronous observe hooks
inline, re-raising their failures. A hook that raises there is not swallowed:
the failure escapes the spawn helper, so the child and its pipes are only
reclaimed if the helper itself terminates and reaps them. These cases pin that
ownership — the child is gone, nothing is left pending on the loop, and the
caller still meets the failure that caused it.

The child blocks without limit: it exists to be terminated, never to finish.
Nothing here waits on the wall clock, because a child that was never reaped is
a failure whether or not the test would have noticed in time.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import scoped, sh
from cuprum.catalogue import ProgramCatalogue, ProjectSettings
from cuprum.context import ScopeConfig
from cuprum.program import Program
from cuprum.sh import StdinInput
from tests.helpers.timeouts import (
    pending_tasks,
    python_interpreter,
    started_pids,
    wait_for_process_death,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent

# Long enough that the child cannot exit on its own, so a run that ends can
# only have ended because the parent terminated it.
_BLOCKING_CHILD = "import time; time.sleep(300)"


class _StartHookError(Exception):
    """Raised by a deliberately failing observe hook in these tests."""


def _blocking_command() -> tuple[sh.SafeCmd, ProgramCatalogue]:
    """Build a blocking Python command and its allowlist catalogue."""
    python_program = Program(python_interpreter())
    project = ProjectSettings(
        name="line-stream-ownership",
        programs=(python_program,),
        documentation_locations=("docs/users-guide.md",),
        noise_rules=(),
    )
    catalogue = ProgramCatalogue(projects=(project,))
    builder = sh.make(python_program, catalogue=catalogue)
    return builder("-c", _BLOCKING_CHILD), catalogue


def _failing_start_hook(events: list[ExecEvent]) -> cabc.Callable[[ExecEvent], None]:
    """Return a synchronous hook that records events and fails on ``start``."""

    def hook(event: ExecEvent) -> None:
        """Record every event, and fail once the child has started."""
        events.append(event)
        if event.phase == "start":
            raise _StartHookError

    return hook


def _failing_before_hook(_command: sh.SafeCmd) -> None:
    """Fail after the plan event and before the child is spawned."""
    raise _StartHookError


class TestStartHookFailureOwnership:
    """A failing ``start`` hook cannot leave the spawned child behind."""

    def test_start_hook_failure_reaps_child_before_it_propagates(self) -> None:
        """The spawn helper terminates the child it already owns.

        ``start`` is emitted after the child exists and before the spawn helper
        returns, so a raising synchronous hook is the one failure that can
        strand a child nothing else holds. The caller must still meet that
        failure, and the child must be reaped by the time it does.
        """
        command, catalogue = _blocking_command()
        events: list[ExecEvent] = []

        async def consume() -> BaseException:
            """Start a line stream whose start hook fails immediately."""
            stream = command.lines()
            try:
                with pytest.raises(_StartHookError) as caught:
                    await anext(stream)
            finally:
                await stream.aclose()
            return typ.cast("BaseException", caught.value)

        with (
            scoped(ScopeConfig(allowlist=catalogue.allowlist)),
            sh.observe(_failing_start_hook(events)),
        ):
            error = asyncio.run(consume())

        (pid,) = started_pids(events)
        assert isinstance(error, _StartHookError), (
            f"the start-hook failure must reach the caller, got {error!r}"
        )
        wait_for_process_death(pid, seconds=5.0, context="a failed start hook")

    def test_start_hook_failure_leaves_no_task_pending(self) -> None:
        """Neither the consumers nor the stdin writer outlive the failure."""
        command, catalogue = _blocking_command()
        events: list[ExecEvent] = []

        async def consume() -> set[asyncio.Task[object]]:
            """Report the loop's tasks once the failure has propagated."""
            stream = command.lines(stdin=StdinInput(text="unread payload"))
            try:
                with pytest.raises(_StartHookError):
                    await anext(stream)
            finally:
                await stream.aclose()
            return pending_tasks()

        with (
            scoped(ScopeConfig(allowlist=catalogue.allowlist)),
            sh.observe(_failing_start_hook(events)),
        ):
            still_pending = asyncio.run(consume())

        assert not still_pending, (
            f"a failed start must leave no task pending, got {still_pending!r}"
        )


class TestPreSpawnHookFailureOwnership:
    """A failure ahead of the spawn cannot strand the tasks already queued."""

    def test_failing_before_hook_still_drains_the_plan_phase_task(self) -> None:
        """The ``plan`` event's scheduled task is drained despite the raise.

        ``plan`` is emitted first, so the async observe hook it scheduled is
        already queued on the tracking list when the before-hook raises. Only
        the iterator's own reconcile drains that list, so a guarded region that
        excluded the pre-spawn emits would leave the task running.
        """
        command, catalogue = _blocking_command()
        events: list[ExecEvent] = []

        async def scheduled_hook(event: ExecEvent) -> None:
            """Stand in for an observe hook scheduled as a background task."""
            events.append(event)
            await asyncio.sleep(0)

        async def consume() -> set[asyncio.Task[object]]:
            """Fail before the spawn and report what the loop still holds."""
            stream = command.lines()
            try:
                with pytest.raises(_StartHookError):
                    await anext(stream)
            finally:
                await stream.aclose()
            return pending_tasks()

        with (
            scoped(
                ScopeConfig(
                    allowlist=catalogue.allowlist,
                    before_hooks=(_failing_before_hook,),
                )
            ),
            sh.observe(scheduled_hook),
        ):
            still_pending = asyncio.run(consume())

        assert not started_pids(events), (
            "the before-hook must fail before any child is spawned"
        )
        assert not still_pending, (
            f"a failed plan must leave no task pending, got {still_pending!r}"
        )
