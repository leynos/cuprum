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
right name to signal: the group was named by a process this run started, so the
name begins as one this run created rather than one it was handed. A run that
does not own the group signals exactly the process it always did.

Beginning as this run's is not the same as still being this run's at the moment
of the signal. The probe is a veto rather than a licence — "no such group" is
conclusive, "yes" is only that some group holds the number.

Two reuse windows follow from that, and they are separate hazards rather than
one. The larger is reuse **before** the probe: the group empties, the kernel
recycles the number onto an unrelated group, and the next probe answers for the
stranger exactly as it would for this run's own. The smaller is reuse
**between** the probe and the signal: they are two separate syscalls, so a
group that empties in the gap is signalled under a name the kernel may already
have recycled. :mod:`cuprum._process_group` states why no process-group
equivalent of ``pidfd`` closes them and how far the kernel's own recycling rate
bounds them.

This module answers *what to signal* and *what to wait for*. The delivery
itself, and everything a teardown that could not reach its target *reports* —
the log record and the execution event, why they travel together, the message
templates, and the bounded metric labels — belongs to
:mod:`cuprum._group_teardown`. The two modules answer different questions, and
the census below is what keeps a reader from having to guess which one holds
the answer they want.

Sequencing those answers belongs to
:func:`cuprum._process_lifecycle._terminate_process_with_wait`, which is where
this module's two questions are actually asked: it decides *when* to signal,
holds the grace period that bounds the wait, and escalates once that bound
expires. The split is not arbitrary. The teardown reports its result as
:class:`~cuprum._process_lifecycle._TerminationOutcome`, and that type has a
second consumer entirely — ``cuprum._process_handle``, which compels a child
without any group question being asked at all — so the outcome belongs beside
the handle-facing helpers that read it rather than here.

Everything in this module is private to the package. It is a split, not a
promotion: what callers imported from ``cuprum._process_lifecycle`` is here.
"""

from __future__ import annotations

import dataclasses as dc
import signal
import time
import typing as typ

from cuprum._group_teardown import (
    GroupTeardownReport,
    report_group_teardown,
    signal_target_or_report,
    unsignalable_report,
)
from cuprum._process_group import (
    _await_group_settlement,
    _await_group_teardown,
    _GroupProbe,
    _probe_group,
)

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc

    from cuprum._pipeline_types import _StageObservation


@dc.dataclass(frozen=True, slots=True)
class _SettlementTarget:
    """How a teardown observes the target it is trying to settle.

    The pair is one value because neither callable means anything alone: a
    caller can construct neither without knowing both, since the two are the
    same target observed two ways — *is_done* answers *has the target already
    settled?* from a flag the caller already holds, and *wait_for_exit*
    answers *wait until it does* when it has not. Every route through the
    teardown carries both to the same places, so they are grouped rather than
    threaded separately through each signature that needs them.

    They are genuinely two answers, though, and the probe asks them in
    different places: :func:`_settlement_of` consults *is_done* because asking
    whether the target has settled must not wait, while
    :func:`_settle_after_escalation` awaits *wait_for_exit* because reaping a
    child that already exited is exactly what it owes the run.
    """

    #: Whether the direct child has been reaped, asked without waiting.
    is_done: cabc.Callable[[], bool]
    #: Await the direct child's exit, reaping it when it is still outstanding.
    wait_for_exit: cabc.Callable[[], cabc.Awaitable[int]]


def _report_teardown(
    observation: _StageObservation | None,
    report: GroupTeardownReport | None,
    error: OSError | None = None,
) -> None:
    """Report one teardown outcome against the execution it acted for.

    The observation is the one paired with the target being signalled, never
    the front of a run-level sequence: a pipeline teardown signals several
    stages, and the stage it is reporting on is whichever one the caller
    reached, not whichever happens to be first. Taking the first would file a
    refusal against stage zero for a signal delivered to stage two.
    """
    if report is None:
        return
    report_group_teardown(observation, report, error)


def _settlement_of(
    process: asyncio.subprocess.Process,
    *,
    owns_group: bool,
    target: _SettlementTarget,
) -> _GroupProbe:
    """Report what the run's teardown target has left to signal.

    An inherited-group run targets the direct child alone, so a completed child
    *is* settlement and the caller's *is_done* answers it — reported as
    ``EMPTY``, since the question is about the target rather than any group.

    An owning run targets the child's whole group, which can outlive the child
    that led it, so a reaped leader is not settlement: a descendant the run
    left behind is exactly the process that keeps an inherited pipe open past a
    graceful child exit, and ignoring it would leak what this policy exists to
    reclaim. What the probe can and cannot establish is stated with the probe
    in :mod:`cuprum._process_group` and in this module's docstring.

    The probe is asked only where
    :func:`~cuprum._group_teardown.signal_target_or_report` would signal.
    A child with no pid has no group name to ask about, and that helper falls
    back to the direct route there, so the run is judged by the direct route
    here rather than by a probe neither of them would make.

    This is a query. It reports what it found, including the group it could not
    signal, and leaves both the decision and the diagnostic to its caller,
    which is where the module's logging convention puts them.

    Returns
    -------
    _GroupProbe
        ``EMPTY`` when the target has nothing left to signal, ``MEMBERS`` when
        it still does, and ``UNSIGNALABLE`` when the group holds members this
        process may not signal and so cannot settle by waiting.
    """
    if owns_group and process.pid is not None:
        return _probe_group(process.pid)
    return _GroupProbe.EMPTY if target.is_done() else _GroupProbe.MEMBERS


async def _settlement(
    process: asyncio.subprocess.Process,
    target: _SettlementTarget,
    *,
    observation: _StageObservation | None,
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

    An unsignalable group ends the wait as promptly as an emptied one, and is
    reported here rather than by the probe that found it: the probe answers,
    this command reports. Waiting on such a group cannot reach its members, so
    the wait returns either way; only the record separates the causes.

    This is a coroutine rather than a function returning an awaitable so the
    waiter is built only once the grace period is actually being waited on.
    Building it eagerly would construct a coroutine that a timed-out
    ``asyncio.wait_for`` then discards un-awaited, which Python reports as a
    ``RuntimeWarning`` from the teardown path.
    """
    if not owns_group or process.pid is None:
        await target.wait_for_exit()
        return
    outcome = await _await_group_teardown(target.wait_for_exit(), process.pid)
    _report_teardown(observation, unsignalable_report(outcome, process.pid))


async def _escalate_after_grace(
    process: asyncio.subprocess.Process,
    target: _SettlementTarget,
    *,
    observation: _StageObservation | None,
    owns_group: bool,
) -> bool:
    """Compel a target that outlived its grace period, or report it settled.

    Settlement is asked again here, and that second ask is the one place a
    fresh probe buys anything an earlier one cannot. The first probe did not
    veto the graceful signal; the whole grace period has passed since, which is
    long enough for the group's last member to exit and for its number to be
    freed. An absent name is conclusive, so this ask is worth making even
    though a *live* answer here proves no more than a live answer ever could —
    it cannot tell this run's group from one the kernel has since handed the
    number to. What the ask removes is the weaker failure: escalating into a
    name that currently answers for nobody, which is a signal at best wasted and
    at worst aimed at whatever the kernel puts there next.

    Returns
    -------
    bool
        Whether the escalation was delivered. ``False`` means the target had
        settled or gone by the time it was considered, or the escalation itself
        raised. Only the last is a kernel refusal, reported through
        :func:`~cuprum._group_teardown.signal_target_or_report`; the others
        are silent by design.
    """
    settled = _settlement_of(process, owns_group=owns_group, target=target)
    _report_teardown(observation, unsignalable_report(settled, process.pid))
    if settled is not _GroupProbe.MEMBERS:
        return False
    if not signal_target_or_report(
        process,
        observation=observation,
        owns_group=owns_group,
        group_signal=signal.SIGKILL,
    ):
        return False
    await _settle_after_escalation(
        process,
        target,
        observation=observation,
        owns_group=owns_group,
    )
    return True


async def _settle_after_escalation(
    process: asyncio.subprocess.Process,
    target: _SettlementTarget,
    *,
    observation: _StageObservation | None,
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
    rather than waited on — with the time the wait actually took, measured
    here, so the figure in the record is the duration the wait ran rather than
    the bound it ran under.

    The child is reaped before the group is probed, which also keeps the probe
    sound: a zombie still counts as a member of its group, so probing first
    would report a group that has in fact already emptied.

    Both ways the capped wait can end without an emptied group are reported,
    and they are distinct facts. The bound expiring leaves members this run
    cannot reap; the probe being refused leaves members this run may not
    signal. Neither is settlement, so neither may be passed over in silence.
    ``_await_group_settlement`` reports the first as ``MEMBERS``, which is what
    tells the two apart; the wait itself is measured around that call, because
    a group that settles just inside its bound and one that runs the bound out
    to its last instant are the same fact to the probe and different ones to
    an operator.
    """
    await target.wait_for_exit()
    if not owns_group or process.pid is None:
        return
    started = time.monotonic()
    outcome = await _await_group_settlement(process.pid)
    settle_s = time.monotonic() - started
    if outcome is _GroupProbe.MEMBERS:
        _report_teardown(
            observation,
            GroupTeardownReport.settlement_timeout(
                process.pid,
                settle_s=settle_s,
            ),
        )
    else:
        _report_teardown(observation, unsignalable_report(outcome, process.pid))


__all__ = [
    "_escalate_after_grace",
    "_report_teardown",
    "_settle_after_escalation",
    "_settlement",
    "_settlement_of",
]
