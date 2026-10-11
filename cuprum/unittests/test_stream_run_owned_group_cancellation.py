"""Cancellation during stream collection of an owned single-command run.

A streamed run has two halves: waiting for the child to exit, and collecting
the stream consumers that carry its output. Only the first half is covered by
the deadline machinery in ``cuprum._subprocess_timeout``; the second half
awaits the consumers directly, outside any timeout.

For an owning run that gap is reachable. The direct child can exit while a
descendant it left behind still holds the run's inherited pipes, so the
consumers never see end-of-file. Cancelling the run at that point releases the
consumers, but nothing has signalled the group — so the descendant survives the
run that spawned it and keeps holding the pipes it inherited.

These tests drive the public ``SafeCmd.run`` boundary against real children,
because the state under test is one only a real process tree can produce: a
reaped leader, a live descendant, and pipes that are open with no writer this
run can see.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import typing as typ

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum.sh import ExecutionContext, ProcessGroupPolicy, RunOutputOptions
from tests.helpers.catalogue import python_catalogue
from tests.helpers.timeouts import (
    descendant_then_exit_argv,
    process_is_running,
    wait_for_pid_file,
    wait_for_process_death,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.sh import Program, SafeCmd

_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups are unavailable on Windows",
)

pytestmark = _posix_only

# The consumers hold the run open until the descendant that owns the pipes is
# gone, so a run whose group was never settled does not fail here — it never
# returns. This bounds that wait, well short of the descendant's own sleep.
_CANCELLED_RUN_BUDGET_S = 20.0


def _recorded_descendant(pid_file: Path) -> int | None:
    """Return the recorded descendant pid, or ``None`` if none was written."""
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


@pytest.fixture
def python_command() -> cabc.Callable[..., SafeCmd]:
    """Provide a ``SafeCmd`` builder for the current interpreter."""
    catalogue, program = python_catalogue()
    return sh.make(program, catalogue=catalogue)


@pytest.fixture
def python_program() -> Program:
    """Provide the allowlisted interpreter the command builder was made from."""
    _catalogue, program = python_catalogue()
    return program


def test_cancelled_owned_run_reclaims_the_descendant_holding_its_pipes(
    python_command: cabc.Callable[..., SafeCmd],
    python_program: Program,
    tmp_path: Path,
) -> None:
    """Cancelling an owned run after its child exits still settles the group.

    The child exits immediately after leaving a ``SIGTERM``-immune descendant
    that inherits the run's stdout and stderr. The run therefore reaches the
    consumer collection with a reaped leader and two readers that can never see
    end-of-file. Cancelling there is the ordinary outcome — a caller giving up
    on output it is not going to get — but the descendant is not the caller's
    to abandon, and only the group can reach it.

    The run is bounded rather than left to hang, because a group that was never
    settled produces a run that does not return at all. That bound is reported
    as the defect it is, not as an infrastructure timeout.
    """
    pid_file = tmp_path / "owned-stream-grandchild.pid"
    command = python_command(*descendant_then_exit_argv(pid_file, exit_code=0))

    async def run_case() -> None:
        """Start the run, wait for its descendant, then cancel it."""
        descendant_pid: int | None = None
        run_task = asyncio.ensure_future(
            command.run(
                output=RunOutputOptions(capture=True, echo=False),
                context=ExecutionContext(
                    process_group=ProcessGroupPolicy.OWN_GROUP,
                ),
            ),
        )
        try:
            with scoped(ScopeConfig(allowlist=frozenset({python_program}))):
                descendant_pid = await asyncio.to_thread(
                    wait_for_pid_file,
                    pid_file,
                    context="the descendant whose pipes the run still holds",
                )
                # The leader exits on its own once the descendant is announced;
                # waiting for that keeps this a cancellation of the *stream
                # collection*, which is the half under test.
                await asyncio.sleep(0.3)
                run_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(run_task, _CANCELLED_RUN_BUDGET_S)
            assert descendant_pid is not None
            await asyncio.to_thread(
                wait_for_process_death,
                descendant_pid,
                context="a cancelled owned run's group teardown",
            )
            assert not process_is_running(descendant_pid), (
                "cancelling an owned run must settle the group its child led, "
                "not merely stop waiting on the consumers: the descendant is "
                "still holding the inherited pipes"
            )
        finally:
            if descendant_pid is None:
                descendant_pid = _recorded_descendant(pid_file)
            if descendant_pid is not None and process_is_running(descendant_pid):
                os.kill(descendant_pid, signal.SIGKILL)

    asyncio.run(run_case())


def test_cancelled_inherited_run_leaves_its_descendant_alone(
    python_command: cabc.Callable[..., SafeCmd],
    python_program: Program,
    tmp_path: Path,
) -> None:
    """The same cancellation under ``INHERIT`` must not reach the descendant.

    Control for the test above, and the property the whole option rests on.
    ``INHERIT`` is the default: the child shares whatever group it was spawned
    into, which in a test run is the group the test runner itself belongs to.
    A teardown that signalled a group here would signal that one, so the
    default has to reach the direct child alone even when a descendant is
    holding the run's pipes.

    The descendant is expected to survive, and that is asserted rather than
    tolerated: it is the visible difference between the two policies applied to
    an identical process tree. Without this control, a fix that signalled the
    group unconditionally would pass the owned test and take the runner with it.
    """
    pid_file = tmp_path / "inherited-stream-grandchild.pid"
    command = python_command(*descendant_then_exit_argv(pid_file, exit_code=0))

    async def run_case() -> None:
        """Cancel an inherited run and assert its descendant is untouched."""
        descendant_pid: int | None = None
        run_task = asyncio.ensure_future(
            command.run(
                output=RunOutputOptions(capture=True, echo=False),
                context=ExecutionContext(
                    process_group=ProcessGroupPolicy.INHERIT,
                ),
            ),
        )
        try:
            with scoped(ScopeConfig(allowlist=frozenset({python_program}))):
                descendant_pid = await asyncio.to_thread(
                    wait_for_pid_file,
                    pid_file,
                    context="the descendant an inherited run must not reach",
                )
                await asyncio.sleep(0.3)
                run_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(run_task, _CANCELLED_RUN_BUDGET_S)
            assert descendant_pid is not None
            assert process_is_running(descendant_pid), (
                "an inherited run shares the group it was spawned into, so its "
                "teardown must reach the direct child alone — signalling the "
                "group here would signal whatever group that is"
            )
        finally:
            if descendant_pid is None:
                descendant_pid = _recorded_descendant(pid_file)
            if descendant_pid is not None and process_is_running(descendant_pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(descendant_pid, signal.SIGKILL)

    asyncio.run(run_case())
