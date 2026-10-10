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
# Back off towards this interval so a group that takes seconds to die is not
# re-probed hundreds of times. The cap stays well under the default grace
# period, so the grace window is still sampled several times over rather than
# being overshot by a single slow poll.
_MAX_PROBE_INTERVAL = 0.1
# The escalation's own bound, and deliberately not derived from
# ``cancel_grace``. ``cancel_grace`` bounds how long a member is *asked* to
# leave; this bounds how long teardown waits to see the result once every
# member has been compelled with ``SIGKILL``. A reaped child that is not a
# group leader can leave its group with an un-reaped adopted descendant — one
# whose parent died before it did — and that member cannot exit until an init
# process reaps it, which is out of this run's hands. Waiting unbounded there
# is what would strand a teardown, so the group wait is capped and the
# outstanding members reported instead.
_POST_KILL_SETTLEMENT_S = 1.0


def _group_has_members(pgid: int) -> bool:
    """Return whether any signalable process still belongs to group *pgid*.

    ``killpg`` with signal ``0`` performs the existence and permission checks
    without delivering anything. ``ProcessLookupError`` means the last member
    has gone. ``PermissionError`` means members exist that this process may not
    signal: neither the grace period nor the ``SIGKILL`` escalation could reach
    them, so waiting longer cannot settle the group and it is reported as
    having no reachable members.

    Returns
    -------
    bool
        Whether the group still holds a member this process could signal.
    """
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


async def _await_group_exit(pgid: int) -> None:
    """Wait until group *pgid* has no signalable members left.

    The interval backs off as the wait continues: a group that dies promptly is
    noticed within one short probe, while one that survives several rounds is
    not re-probed at full frequency for as long as it takes. A process group
    has no completion event to await — nothing signals the kernel to wake us
    when the last member is reaped — so polling is the only way to observe it,
    which is why this sleeps in a loop at all.
    """
    interval = _PROBE_INTERVAL
    while _group_has_members(pgid):
        await asyncio.sleep(interval)
        interval = min(interval * 2, _MAX_PROBE_INTERVAL)


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
