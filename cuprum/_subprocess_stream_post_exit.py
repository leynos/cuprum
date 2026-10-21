"""The post-exit phase of a streamed single-command run.

Split from ``cuprum._subprocess_stream_run`` because the boundary between the
child's exit and its completed gather is a phase of its own with its own
hazards, and it is the part of that module whose correctness is least obvious
from reading a run loop top to bottom.

Every await in this phase can be cancelled and each needs the same aftermath,
so each one is written to hand its failure to a coordinator rather than to
repair it locally. Two of them are here:

- :func:`_settle_owned_group_before_draining` terminates a group the run owns,
  because the direct child exiting is not settlement while a descendant still
  holds the write end of the run's pipes.
- :func:`_await_stdin_writer` waits for the writer that closes the child's
  stdin, which must finish before the consumers are gathered.

The third, the exit wait itself, lives in
``cuprum._subprocess_stream_exit`` and this module does not restate it. A
second copy here would be a binding no call path resolves, and the run module
imports the one from the exit module.

``cuprum._subprocess_stream_run`` re-imports both helpers, and that re-import is
load-bearing rather than cosmetic: the callers of ``_await_stdin_writer`` and
``_settle_owned_group_before_draining`` remained behind in the run module, so
it is *its* globals those callers read. Re-importing keeps the names bound
there, which is what lets the teardown-boundary tests keep patching them on the
run module — where the production call path still resolves them.

This module is a leaf with respect to the run module: it imports nothing from
it, so the re-export direction is one-way and no cycle is introduced.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._process_lifecycle import _terminate_all_shielded
from cuprum._subprocess_stdin_reclaim import _settle_stdin_writer
from cuprum._teardown_policy import _TeardownPolicy

if typ.TYPE_CHECKING:
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_wait_types import _RunTaskOwnership


async def _settle_owned_group_before_draining(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
) -> None:
    """Terminate the run's owned group so the drain that follows can reach EOF.

    Once the direct child has exited, this module still has stream consumers to
    collect. For an owning run that exit is not settlement: a descendant the
    child left behind is still in the group, still holds the write end of the
    run's pipes, and no signal aimed at the leader can reach it. Draining first
    would spend the bounded capture grace on readers that cannot reach EOF and
    then discard output the descendant was still producing.

    So the group is settled before the drain that follows, and it lives here
    rather than beside any one run shape because four of them call it: the
    streamed run before its gather, the owned-handle path before its gather,
    the line stream before its post-exit drain, and the direct run when its
    writer could not be reclaimed in time.

    Only the exit paths that are already tearing a run down settle. The
    success path of a direct run, whose child exited within its budget and
    whose writer finished, does not: descendants a caller deliberately left
    running are the caller's to manage, and killing the group there would
    contradict what ``run()`` promises. The direct run's own teardown reclaims
    the writer *first* and settles the group second, so the pair shares one
    shield; that order is the opposite of the gather-based paths, where
    settlement must precede a drain that would otherwise wait for EOF the
    descendant never provides.

    The teardown is shielded like every other one on a failure path, so a
    cancellation arriving during its grace period cannot skip the escalation.

    A run that does not own its group has nothing to do here — its only
    teardown target is the direct child, which has already exited on every path
    reaching this helper — so the call is skipped rather than made a no-op.
    """
    if not execution.owns_process_group:
        return
    await _terminate_all_shielded(
        (process,),
        _TeardownPolicy(
            execution.ctx.cancel_grace,
            owns_group=True,
        ),
        observations=(execution.observation,),
    )


async def _await_stdin_writer(tasks: _RunTaskOwnership) -> None:
    """Await the stdin writer, letting a failure reach the run's cleanup.

    The writer must finish before the consumers are gathered: it is what closes
    the child's stdin, and a consumer waiting on a child still blocked reading
    is a consumer waiting for a write that has not happened. A failure or
    cancellation here propagates to the caller's post-exit boundary, which owns
    the whole cleanup — group teardown included. Cleaning up here instead would
    be the narrower repair that leaves the group unsettled.

    The wait is shielded. Awaiting the writer task makes it this await's
    ``_fut_waiter``, and ``Task.cancel()`` forwards — so without the shield the
    boundary is woken only after the writer has finished dying, including the
    close grace it bounds. The shield returns the cancellation at once instead.
    It is a latency improvement and *not* what makes the run terminate: the
    writer's own bounded settle does that (ADR 021 records the measurements).

    The shielded await is a *settle* rather than a bare await of the task, and
    that is what supplies the bound the shield only protects. The writer's
    ``_close_stdin`` grace bounds a write already in flight, but it is reached
    only once the producer has finished producing; a producer parked in
    ``anext`` never gets there, so a bare await would sit on this line with the
    child already reaped and outlast the run's own deadline. The settle window
    plus the cancel it falls back to is what makes the wait terminate in that
    case, and it reclaims the task rather than leaving it parked.
    """
    if tasks.stdin_task is None:
        return
    await asyncio.shield(_settle_stdin_writer(tasks.stdin_task))


__all__ = [
    "_await_stdin_writer",
    "_settle_owned_group_before_draining",
]
