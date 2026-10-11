"""Observe when a POSIX process group has emptied.

An owning run signals the group its child leads rather than the direct child,
so teardown has to know when every member of that group is gone. A null signal
to the group answers that without disturbing it, and because a group ceases to
exist once its last member is reaped, the probe is definitive: there is no
descendant table to keep and no way to count a process outside the group.

Everything here observes and reports its answer; nothing here logs, and nothing
here raises for a state the kernel reported. A probe that could not signal the
group is a fact about the environment, not about the group, so it is returned
as its own outcome for the caller to interpret. That is what keeps the
settlement query in :mod:`cuprum._process_signal` free of side-effects: the
caller that acts on the answer is also the caller that reports it, which is
where the module's own diagnostic-logging convention puts the report.

What the probe cannot do is attribute its answer. It reports whether the
*number* is held, never whether the group holding it is this run's; the kernel
offers nothing stronger, because every ``killpg`` a process can make takes a
number. A probe that has stopped answering proves the group has emptied and is
conclusive. A probe that still answers proves only that some group holds the
name — the original could have emptied and been re-used before the probe ran,
which no probe can detect. The probe is a veto that did not fire, not a licence
to signal on the strength of the name alone.

Two windows follow, and they are separate hazards rather than one. The larger
is reuse **before** the probe: the group empties, the kernel recycles the
number onto an unrelated group, and the next probe answers for the stranger
exactly as it would for this run's own. Nothing here can detect that. The
smaller is reuse **between** the probe and the signal: they are two separate
syscalls, so a group that empties in the gap is signalled under a name the
kernel may already have recycled.

Neither window is closable here. A lifetime-safe handle on the target would
close both, which is what ``pidfd`` gives for a single process, but ``pidfd``
is per-pid — ``os.pidfd_open`` and ``signal.pidfd_send_signal`` take a process
identifier — and no process-group equivalent exists on this platform. The
residual is therefore the one ``Popen`` documents between its own liveness
check and its ``os.kill`` (bpo-38630), bounded in practice by how long the
kernel takes to recycle an identifier.
"""

from __future__ import annotations

import asyncio
import enum
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


class _GroupProbe(enum.Enum):
    """What a null signal to a process group reported about that group.

    The three outcomes are kept apart rather than collapsed to a truth value
    because only two of them are answers about the group. ``MEMBERS`` and
    ``EMPTY`` say whether the group holds a member; ``UNSIGNALABLE`` says the
    probe could not find out, because the group — or one of its members — is
    owned by another user. Reporting that as "empty" would let an environmental
    permission failure masquerade as a clean teardown, and reporting it as
    "members" would make teardown wait out a process it can never end.
    """

    MEMBERS = enum.auto()
    EMPTY = enum.auto()
    UNSIGNALABLE = enum.auto()


def _probe_group(pgid: int) -> _GroupProbe:
    """Report what a null signal to group *pgid* says about that group.

    ``killpg`` with signal ``0`` performs the existence and permission checks
    without delivering anything. ``ProcessLookupError`` means the last member
    has gone, so the group has ceased to exist. ``PermissionError`` means
    members exist that this process may not signal: neither the grace period
    nor the ``SIGKILL`` escalation could reach them, so waiting longer cannot
    settle the group, but the group is *not* gone.

    This is a query, so it does not log or raise. The caller that decides what
    an unreachable group means for a teardown is also the caller that reports
    it; see :mod:`cuprum._process_signal`.

    Returns
    -------
    _GroupProbe
        Which of the three outcomes the kernel reported.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return _GroupProbe.EMPTY
    except PermissionError:
        return _GroupProbe.UNSIGNALABLE
    return _GroupProbe.MEMBERS


async def _await_group_exit(pgid: int) -> _GroupProbe:
    """Wait until group *pgid* has no signalable members left.

    The interval backs off as the wait continues: a group that dies promptly is
    noticed within one short probe, while one that survives several rounds is
    not re-probed at full frequency for as long as it takes. A process group
    has no completion event to await — nothing signals the kernel to wake us
    when the last member is reaped — so polling is the only way to observe it,
    which is why this sleeps in a loop at all.

    The permission outcome stops the wait exactly as an emptied group does, and
    for the reason the poll exists: neither can be changed by waiting longer.
    Which of the two ended the wait is returned rather than discarded, so the
    caller can report a group it could not reach.

    Returns
    -------
    _GroupProbe
        ``EMPTY`` or ``UNSIGNALABLE``. Never ``MEMBERS``: the poll only returns
        once the group has stopped reporting one.
    """
    interval = _PROBE_INTERVAL
    while True:
        outcome = _probe_group(pgid)
        if outcome is not _GroupProbe.MEMBERS:
            return outcome
        await asyncio.sleep(interval)
        interval = min(interval * 2, _MAX_PROBE_INTERVAL)


async def _await_group_teardown(
    child_exit: cabc.Awaitable[int],
    pgid: int,
) -> _GroupProbe:
    """Wait for the direct child to be reaped and its group to empty.

    Both waits are required together, and neither implies the other. The group
    still reports a member while the child is an un-reaped zombie, so the group
    probe cannot settle before the child's exit is collected; and the child's
    exit says nothing about descendants it left behind, so it cannot settle the
    group on its own.

    The group poller is cancelled if the child's waiter fails. ``gather``
    propagates the first exception without cancelling its siblings, which would
    otherwise leave a poll loop running for the lifetime of the process.

    Returns
    -------
    _GroupProbe
        How the group's wait ended, so an unsignalable group can be reported
        by the caller rather than mistaken for one that emptied.
    """
    group_task = asyncio.ensure_future(_await_group_exit(pgid))
    try:
        _, outcome = await asyncio.gather(child_exit, group_task)
    finally:
        if not group_task.done():
            group_task.cancel()
        await asyncio.gather(group_task, return_exceptions=True)
    return outcome


async def _await_group_settlement(pgid: int) -> _GroupProbe:
    """Wait for group *pgid* to empty, capped by the post-``SIGKILL`` bound.

    The group's exit is not wholly this run's to observe, so this wait cannot
    be unbounded: a member whose parent died first can be re-parented to an
    init process and is only reaped when something outside this run gets to it.
    Waiting for that without a bound is what would let a teardown run past its
    grace period forever. ``MEMBERS`` is returned when the bound expires with
    the group still populated, which is the one outcome an uncapped
    :func:`_await_group_exit` cannot produce, so a caller can distinguish an
    expired bound from a refused probe without consulting a clock. How long the
    wait ran is a separate question this function does not answer; a caller
    that wants that duration measures the await itself.

    Returns
    -------
    _GroupProbe
        ``MEMBERS`` when the bound expired with the group still populated, and
        the underlying probe's outcome otherwise.
    """
    try:
        async with asyncio.timeout(_POST_KILL_SETTLEMENT_S):
            return await _await_group_exit(pgid)
    except TimeoutError:
        return _GroupProbe.MEMBERS
