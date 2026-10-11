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

The sibling module covers a cancellation that lands *between* the child's exit
and the gather, at each of the three awaits that precede it. The tools both
modules need — the allowlisted interpreter builder, the descendant bookkeeping,
and the bounded waits — live in ``_post_exit_teardown_support``.
"""

from __future__ import annotations

import asyncio
import sys
import typing as typ

import pytest

from cuprum.sh import ExecutionContext, ProcessGroupPolicy, RunOutputOptions
from cuprum.unittests._post_exit_teardown_support import (
    owned_builder,
    reap_descendant,
    settle,
)
from tests.helpers.timeouts import (
    descendant_then_exit_argv,
    process_is_running,
    wait_for_pid_file,
    wait_for_process_death,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.sh import SafeCmd

# The fixture is imported rather than defined here; declaring it records that
# pytest resolves the name at collection time rather than leaving it to be
# pruned as an unused import.
__all__ = ["owned_builder"]

_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups are unavailable on Windows",
)

pytestmark = _posix_only


def test_cancelled_owned_run_reclaims_the_descendant_holding_its_pipes(
    owned_builder: cabc.Callable[..., SafeCmd],
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
    command = owned_builder(*descendant_then_exit_argv(pid_file, exit_code=0))

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
            await settle(run_task)
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
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())


def test_cancelled_inherited_run_leaves_its_descendant_alone(
    owned_builder: cabc.Callable[..., SafeCmd],
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
    command = owned_builder(*descendant_then_exit_argv(pid_file, exit_code=0))

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
            descendant_pid = await asyncio.to_thread(
                wait_for_pid_file,
                pid_file,
                context="the descendant an inherited run must not reach",
            )
            await asyncio.sleep(0.3)
            run_task.cancel()
            await settle(run_task)
            assert process_is_running(descendant_pid), (
                "an inherited run shares the group it was spawned into, so its "
                "teardown must reach the direct child alone — signalling the "
                "group here would signal whatever group that is"
            )
        finally:
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())
