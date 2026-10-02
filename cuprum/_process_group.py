"""Observe when a POSIX process group has emptied.

An owning run signals the group its child leads rather than the direct child,
so teardown has to know when every member of that group is gone. A null signal
to the group answers that without disturbing it, and because a group ceases to
exist once its last member is reaped, the probe is definitive: there is no
descendant table to keep and no way to count a process outside the group.
"""

from __future__ import annotations

import asyncio
import os
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# Short enough that a teardown which settles immediately pays at most one
# probe, long enough that a group outliving the grace period is noticed
# without spinning.
_PROBE_INTERVAL = 0.01


def _group_has_members(pgid: int) -> bool:
    """Return whether any signalable process still belongs to group *pgid*.

    ``killpg`` with signal ``0`` performs the existence and permission checks
    without delivering anything. ``ProcessLookupError`` means the last member
    has gone. ``PermissionError`` means members exist that this process may not
    signal: neither the grace period nor the ``SIGKILL`` escalation could reach
    them, so waiting longer cannot settle the group and it is reported as
    having no reachable members.
    """
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


async def _await_group_exit(pgid: int) -> None:
    """Wait until group *pgid* has no signalable members left."""
    while _group_has_members(pgid):
        await asyncio.sleep(_PROBE_INTERVAL)


async def _await_group_teardown(
    child_exit: cabc.Awaitable[int],
    pgid: int,
) -> None:
    """Wait for the direct child to be reaped and its group to empty.

    Both waits are required together, and neither implies the other. The group
    still reports a member while the child is an un-reaped zombie, so the group
    probe cannot settle before the child's exit is collected; and the child's
    exit says nothing about descendants it left behind, so it cannot settle the
    group on its own.

    The group poller is cancelled if the child's waiter fails. ``gather``
    propagates the first exception without cancelling its siblings, which would
    otherwise leave a poll loop running for the lifetime of the process.
    """
    group_task = asyncio.ensure_future(_await_group_exit(pgid))
    try:
        await asyncio.gather(child_exit, group_task)
    finally:
        if not group_task.done():
            group_task.cancel()
        await asyncio.gather(group_task, return_exceptions=True)
