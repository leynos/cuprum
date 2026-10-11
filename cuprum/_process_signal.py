"""Choose the target of a teardown signal, and settle it afterwards.

Teardown has two closely coupled decisions that are neither the policy that
asks for it nor the bookkeeping that records it: *what* a signal is delivered
to, and *what* a completed teardown is waiting for. Both follow from one fact
about the run — whether it spawned its child as the leader of a new process
group — so they live together here, leaving
``cuprum._process_lifecycle`` to own the grace period and the escalation that
use them.

A child spawned with ``start_new_session=True`` is the leader of both a new
session and a new process group, so its group identifier equals its process
identifier. That equality is what makes ``os.killpg(process.pid, sig)`` the
right name to signal here: the group was named by a process this run started,
so the name begins as one this run created rather than one it was handed. A run
that does not own the group signals exactly the process it always did.

Beginning as this run's is not the same as still being this run's at the moment
of the signal, and the rest of this docstring is about that gap. Nothing below
should be read as a licence to signal on the strength of the name alone.

What the name does *not* carry is a guarantee that no unrelated group can ever
hold it. The kernel keeps a group addressable for as long as it has a member,
but once the group has emptied its identifier is an ordinary free slot the
kernel may hand to anyone. The liveness probe reads the kernel's answer about
the *number*, and the kernel has no way to answer about the *group*: every
``killpg`` a process can make takes a number, so there is nothing to ask that
would distinguish this run's group from a stranger's that inherited the number.
That makes the probe a veto rather than a licence — an answer of "no such
group" is conclusive, and an answer of "yes" is only that some group holds the
number, never that the group is this run's.

Two windows follow, and they are separate hazards rather than one.

The larger one is reuse **before** the probe: the group empties, the kernel
recycles the number onto an unrelated group, and the next probe answers for the
stranger exactly as it would for this run's own. Nothing here can detect that,
and the signal that follows is delivered to the stranger. Every probe in this
module has it, and no stronger probe is available to be asked for. The probe
that guards the escalation is the one exception, in the weaker direction only:
it fires *after* the group was known live, so it can see that the name has
since stopped answering for anyone — which is conclusive — but it cannot
establish that an answering name is this run's.

The smaller one is reuse **between** the probe and the signal: they are two
separate syscalls, so a group that empties in the gap is signalled under a name
the kernel may already have recycled.

Neither window is closable here. A lifetime-safe handle on the target would
close both, which is what ``pidfd`` gives for a single process, but ``pidfd``
is per-pid — ``os.pidfd_open`` and ``signal.pidfd_send_signal`` take a process
identifier — and no process-group equivalent exists on this platform. The
residual is therefore the one ``Popen`` documents between its own liveness
check and its ``os.kill`` (bpo-38630), bounded in practice by how long the
kernel takes to recycle an identifier.

Everything in this module is private to the package. It is a split, not a
promotion: callers that previously imported these helpers from
``cuprum._process_lifecycle`` now import them from here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import typing as typ

from cuprum._process_group import (
    _POST_KILL_SETTLEMENT_S,
    _await_group_exit,
    _await_group_teardown,
    _group_has_members,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._teardown_policy import _TeardownPolicy

_LOGGER = logging.getLogger(__name__)


def _teardown_is_settled(
    process: asyncio.subprocess.Process,
    *,
    owns_group: bool,
    is_done: cabc.Callable[[], bool],
) -> bool:
    """Return whether the run's teardown target has nothing left to signal.

    An inherited-group run targets the direct child alone, so a completed child
    *is* settlement and the caller's *is_done* answers it.

    An owning run targets the child's whole group, and that group can outlive
    the child that led it. The kernel holds a group addressable for as long as
    it has a member, so a probe that answers says a group is still there, and
    it knows something *is_done* cannot: whether a descendant is still in the
    group. Such a descendant is exactly the process that keeps an inherited
    pipe open past a graceful child exit, so treating the reaped leader as
    settlement would leak the very descendant this policy exists to reclaim.

    What the probe cannot do is attribute the answer. It reports whether the
    *number* is held, never whether the group holding it is this run's; the
    kernel offers nothing stronger, because every ``killpg`` a process can make
    takes a number. So the two answers are not equally informative. A probe
    that has stopped answering proves the group has emptied and is conclusive.
    A probe that still answers proves only that some group holds the name — the
    original could have emptied and been re-used before the probe ran, which no
    probe can detect. The probe is a veto that did not fire, not a licence.

    The weaker answer is still worth having, which is why settlement rests on
    it rather than on *is_done*: a two-way guarantee is not needed for the
    deduction above, since a group with a live descendant answers and this run
    has no other way to ask. See the module docstring for the residual this
    leaves and why no handle closes it.

    The probe is asked only where :func:`_signal_child` would signal the group.
    A child with no pid has no group name to ask about, and that helper falls
    back to the direct route there, so settlement is decided the same way.

    Returns
    -------
    bool
        Whether the teardown target has nothing left to signal, so no signal
        need be sent.
    """
    if owns_group and process.pid is not None:
        return not _group_has_members(process.pid)
    return is_done()


def _signal_child(
    process: asyncio.subprocess.Process,
    *,
    group_signal: int,
    direct: cabc.Callable[[], None],
    owns_group: bool,
) -> None:
    """Signal the child alone, or the whole group when this run owns it.

    *owns_group* is only true for a child spawned with
    ``start_new_session=True``, which makes that child the leader of its own
    session and process group. The group is therefore named by the child's own
    process identifier: no ``os.getpgid`` lookup is needed, and the identifier
    is one this run created rather than one it was handed.

    The name identifies a group this run made; it does not by itself guarantee
    that group still exists, nor that it is still the group holding the name.
    The caller reaches this function only after the
    settlement probe has declined to veto — but a probe that answers cannot
    attribute the name to this run. The original group could have emptied and
    the kernel recycled the number onto a stranger's group *before* the probe
    ran, and the probe and this signal are separate syscalls besides, so a
    group that empties between them leaves a name the kernel may already have
    re-used. The module docstring records both windows, why no process-group
    equivalent of ``pidfd`` closes them, and how far the kernel's own recycling
    rate bounds them.

    Signalling the group rather than the direct child is what lets a child's
    descendants reach end-of-file on inherited pipes; a descendant that left
    the group deliberately is outside this call's reach, exactly as
    :class:`~cuprum.sh.ProcessGroupPolicy` documents.

    *direct* is the child-only route — ``process.terminate`` or
    ``process.kill`` — passed in rather than selected here so a run that does
    not own the group signals exactly the process it always did. Both
    ``os.killpg`` and those two raise ``ProcessLookupError`` when the target
    has already gone, so callers keep one handler across either route.
    """
    if owns_group and process.pid is not None:
        os.killpg(process.pid, group_signal)
        return
    direct()


async def _settlement(
    process: asyncio.subprocess.Process,
    wait_for_exit: cabc.Callable[[], cabc.Awaitable[int]],
    *,
    owns_group: bool,
) -> None:
    """Wait for the run's teardown target to settle.

    An inherited-group run targets the direct child alone, exactly as before.
    An owning run targets the child's whole group, so the direct child exiting
    is not settlement: the group is only settled once it holds no signalable
    member left. Anchoring the grace period on that — rather than on the child
    — is what lets the escalation reach a descendant the direct child left
    behind. A leader that exits promptly on ``SIGTERM`` would otherwise end the
    grace period before the group was ever compelled, and a descendant immune
    to ``SIGTERM`` would outlive the run that spawned it.

    Both waits are composed rather than replaced so the direct child's exit is
    still awaited, and therefore reaped, on every route.

    This is a coroutine rather than a function returning an awaitable so the
    waiter is built only once the grace period is actually being waited on.
    Building it eagerly would construct a coroutine that a timed-out
    ``asyncio.wait_for`` then discards un-awaited, which Python reports as a
    ``RuntimeWarning`` from the teardown path.
    """
    if not owns_group or process.pid is None:
        await wait_for_exit()
        return
    await _await_group_teardown(wait_for_exit(), process.pid)


async def _terminate_process_with_wait(
    process: asyncio.subprocess.Process,
    *,
    policy: _TeardownPolicy,
    is_done: cabc.Callable[[], bool],
    wait_for_exit: cabc.Callable[[], cabc.Awaitable[int]],
) -> bool:
    """Terminate a process and report whether its waiter completed.

    The two-phase grace is unchanged whether the run owns the child's group or
    not: the first phase asks every member of the target to exit, the second
    compels whichever of them outlived the grace period. Only the target of
    those signals, and of the settlement they wait on, differs, and
    :func:`_signal_child` and :func:`_settlement` are the two places that
    decide it.

    Settled is decided by :func:`_teardown_is_settled` rather than by *is_done*
    alone. For an inherited-group run the two are the same question, but an
    owning run's target is the child's whole group, and that group survives the
    child that led it: a descendant left holding an inherited pipe is still a
    member after the child has been reaped. Short-circuiting on the child alone
    would leave that descendant running and the run's pipes held open, which is
    the failure this policy exists to prevent.

    Returns
    -------
    bool
        Whether the target was signalled and its waiter ran to completion.
        ``False`` means it had already settled, or had gone before it could be
        signalled.
    """
    grace_period = max(0.0, policy.grace_period)
    owns_group = policy.owns_group_for(0)
    if _teardown_is_settled(process, owns_group=owns_group, is_done=is_done):
        return False
    try:
        _signal_child(
            process,
            group_signal=signal.SIGTERM,
            direct=process.terminate,
            owns_group=owns_group,
        )
    except (ProcessLookupError, OSError):
        return False
    try:
        await asyncio.wait_for(
            _settlement(process, wait_for_exit, owns_group=owns_group),
            grace_period,
        )
    except asyncio.TimeoutError:  # ruff: ignore[timeout-error-alias] - explicit asyncio timeout needed
        return await _escalate_after_grace(
            process,
            wait_for_exit,
            owns_group=owns_group,
            is_done=is_done,
        )
    return True


async def _escalate_after_grace(
    process: asyncio.subprocess.Process,
    wait_for_exit: cabc.Callable[[], cabc.Awaitable[int]],
    *,
    owns_group: bool,
    is_done: cabc.Callable[[], bool],
) -> bool:
    """Compel a target that outlived its grace period, or report it settled.

    Settlement is asked again here, and that second ask is the one place a
    fresh probe buys anything an earlier one cannot. The first probe did not
    veto the graceful signal; the whole grace period has passed since, which is
    long enough for the group's last member to exit and for its number to be
    freed. An absent name is conclusive, so this ask is worth making even
    though a *live* answer here proves no more than a live answer ever could —
    it cannot tell this run's group from one the kernel has since handed the
    number to.
    What the ask removes is the weaker failure: escalating into a name that
    currently answers for nobody, which is a signal at best wasted and at worst
    aimed at whatever the kernel puts there next.

    Returns
    -------
    bool
        Whether the escalation was delivered. ``False`` means the target had
        settled by the time it was considered, or had gone before the signal
        could be sent.
    """
    if _teardown_is_settled(process, owns_group=owns_group, is_done=is_done):
        return False
    try:
        _signal_child(
            process,
            group_signal=signal.SIGKILL,
            direct=process.kill,
            owns_group=owns_group,
        )
    except (ProcessLookupError, OSError):
        return False
    await _settle_after_escalation(process, wait_for_exit, owns_group=owns_group)
    return True


async def _settle_after_escalation(
    process: asyncio.subprocess.Process,
    wait_for_exit: cabc.Callable[[], cabc.Awaitable[int]],
    *,
    owns_group: bool,
) -> None:
    """Reap the escalated target, bounding only the group's wait.

    The two halves are awaited in turn and bounded differently, because they
    end for different reasons. The direct child's exit is this run's to wait
    for and always arrives — the escalation that preceded this call guarantees
    it — so it is awaited without a bound. A bound there would mean tearing
    down while the process this run spawned was still un-reaped.

    The child's wait is idempotent, so it is awaited here unconditionally
    rather than only when the grace phase cannot have reaped it. The grace
    phase is abandoned un-started whenever it times out — which is exactly the
    route to this function — so the child it would have reaped is still
    outstanding, and re-awaiting a child that already exited returns its
    published code without building a second waiter.

    The group's exit is not wholly this run's to observe. A member whose parent
    died first can be re-parented to an init process, and its own exit is
    recorded only when something outside this run reaps it. Waiting for that
    without a bound is what would let a teardown run past its grace period
    forever, so the group wait is capped and outstanding members are reported
    rather than waited on.

    The child is reaped before the group is probed, which also keeps the probe
    sound: a zombie still counts as a member of its group, so probing first
    would report a group that has in fact already emptied.
    """
    await wait_for_exit()
    if not owns_group or process.pid is None:
        return
    try:
        async with asyncio.timeout(_POST_KILL_SETTLEMENT_S):
            await _await_group_exit(process.pid)
    except TimeoutError:
        _LOGGER.warning(
            "cuprum.process_group_settlement_timeout pid=%s bound_s=%s; "
            "the group still holds members this run cannot reap",
            process.pid,
            _POST_KILL_SETTLEMENT_S,
        )
