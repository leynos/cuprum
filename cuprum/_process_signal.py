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
identifier. That equality is what makes ``os.killpg(process.pid, sig)`` safe
here: the group is named by a process this run started, and no other process
can be in a group this run addresses but did not create. A run that does not
own the group signals exactly the process it always did.

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
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._teardown_policy import _TeardownPolicy

_LOGGER = logging.getLogger(__name__)


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
    process identifier: no ``os.getpgid`` lookup is needed, and no other
    process can be in a group this run addresses but did not create.

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

    *is_done* still short-circuits on the direct child, which is deliberate.
    While that child is un-reaped its identifier is unambiguously this run's,
    so the group name ``pid == pgid`` is too. Once the child has been reaped
    that name may already have been recycled, so signalling it could reach a
    group this run never created; an owned teardown therefore stops there
    rather than trading a leaked descendant for signalling a stranger.

    Returns
    -------
    bool
        Whether the target was signalled and its waiter ran to completion.
        ``False`` means it had already settled, or had gone before it could be
        signalled.
    """
    grace_period = max(0.0, policy.grace_period)
    owns_group = policy.owns_group_for(0)
    if is_done():
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
