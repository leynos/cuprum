"""Process termination and the shared cancellation-safe cleanup primitive.

Termination sends SIGTERM, waits out a grace period, then escalates to
SIGKILL and reaps the exit, whether for one process
(``_terminate_process``, ``_terminate_process_with_wait``) or a whole
pipeline (``_cleanup_pipeline_on_error``, ``_terminate_timed_out_stages``,
``_terminate_pipeline_remaining_stages``). ``_shielded_cleanup`` underlies all
of that: it is the cancellation-safe primitive shared by the pipeline paths
(``_pipeline_internals``, ``_pipeline_collect``) and the single-command
subprocess paths (``_subprocess_execution``, ``_subprocess_wait``,
``_command_internals._execute_with_hooks``). A bare ``asyncio.shield`` is not
enough on its own: it stops cancellation reaching the inner coroutine, but
the awaiting coroutine resumes immediately, so a caller's ``CancelledError``
propagates while cleanup is still running. ``_shielded_cleanup`` instead
retries the wait under a shield until the owned task is done, absorbing
however many cancellations arrive before re-raising. ``_run_shielded_step``
refines that primitive for callers that must run *two* cleanups and decide
afterwards which failure the caller is owed: it reports how the step ended,
distinguishing a cancellation from an ordinary failure, so the deciding stays
with the caller rather than being duplicated into every cleanup.

The pipeline waiter decides when fail-fast teardown is necessary; this module
owns the subprocess handles and executes that decision alongside timeout and
error cleanup. Starting that pipeline — and cleaning up the resources left by
a partial spawn — belongs to ``cuprum._pipeline_spawn``. What a teardown
signal is delivered to, and what a completed teardown waits for, belongs to
``cuprum._process_signal``: this module decides *when* to signal, that one
decides *what* the signal reaches.

``_terminate_process_with_wait`` sequences the two. It reports a
:class:`_TerminationOutcome` rather than a bare flag, because the stops a
process handle performs — ``cuprum._process_handle``, which compels a child
with no group question to ask — read three of its four facts independently.
Only the pipeline's own selection needs ``waiter_completed`` alone, which is
the single ``bool`` the earlier signature returned.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import typing as typ

from cuprum._group_teardown import (
    signal_target_or_report,
)
from cuprum._process_exit import _await_process_exit
from cuprum._process_signal import (
    _escalate_after_grace,
    _members_remain_to_signal,
    _settlement,
    _SettlementTarget,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import _StageObservation
    from cuprum._teardown_policy import _TeardownPolicy


async def _terminate_process(
    process: asyncio.subprocess.Process,
    policy: _TeardownPolicy,
    *,
    observation: _StageObservation | None = None,
) -> _TerminationOutcome:
    """Terminate a running process, escalating to kill after the grace period."""
    return await _terminate_process_with_wait(
        process,
        policy=policy,
        target=_SettlementTarget(
            is_done=lambda: process.returncode is not None,
            wait_for_exit=lambda: _await_process_exit(process),
        ),
        observation=observation,
    )


class _TerminationOutcome(typ.NamedTuple):
    """What a cooperative stop did to the child, and how the wait for it ended.

    Three facts that a single ``bool`` used to flatten, kept apart because they
    answer different questions and can disagree. ``delivered`` is about the
    child: whether any signal was actually sent to it. ``escalated`` is about
    the grace window: whether it closed with the child still running. And
    ``waiter_completed`` is about the caller's own waiter: whether it returned
    rather than being left pending.

    They disagree at the edges. A forced signal that raises
    ``ProcessLookupError`` — the child died in the instant between the window
    closing and the kill — leaves ``delivered`` and ``escalated`` both true
    while ``waiter_completed`` is false, because the SIGTERM that started the
    window did reach a child, and the waiter that was watching it may still be
    the caller's to await. Reporting one ``bool`` here would have to pick one
    of those questions and silently answer the others wrong.

    ``refused`` is the fourth fact and the only one this module never sets. The
    stop helpers cannot tell a delivery the platform would not make from a
    child that had already gone, because both reach them as the same
    ``ProcessLookupError``; only the child's own status separates them, and the
    handle reads that. It defaults to ``False`` so the helpers keep reporting
    exactly what they observed, and the one caller that can weigh the two
    against each other rewrites it.
    """

    delivered: bool
    escalated: bool
    waiter_completed: bool
    refused: bool = False


_NO_STOP = _TerminationOutcome(
    delivered=False,
    escalated=False,
    waiter_completed=False,
)
"""What a stop reports when it sent nothing to the child.

Both of this module's early returns mean the same thing — the child was not
running, or the signal never reached it — and neither the grace window nor the
waiter was ever engaged.
"""

_TERMINATED_IN_GRACE = _TerminationOutcome(
    delivered=True,
    escalated=False,
    waiter_completed=True,
)
"""The cooperative ending: the child took the graceful signal and exited in time."""

_KILLED_AFTER_GRACE = _TerminationOutcome(
    delivered=True,
    escalated=True,
    waiter_completed=True,
)
"""The escalated ending: the grace window ran out with the child alive."""

_KILLED_AFTER_GRACE_UNWAITED = _TerminationOutcome(
    delivered=True,
    escalated=True,
    waiter_completed=False,
)
"""The escalated ending whose reaping wait never returned.

The graceful signal reached the child and the window did run out, but the
forced signal's own ``ProcessLookupError`` means the exit this caller would
have waited for was collected elsewhere — see the class docstring for why the
three facts disagree here.
"""


async def _terminate_process_with_wait(
    process: asyncio.subprocess.Process,
    *,
    policy: _TeardownPolicy,
    target: _SettlementTarget,
    observation: _StageObservation | None = None,
) -> _TerminationOutcome:
    """Terminate a process and report what the stop did to it.

    The two-phase grace is unchanged whether the run owns the child's group or
    not: the first phase asks every member of the target to exit, the second
    compels whichever of them outlived the grace period. Only the target of
    those signals, and of the settlement they wait on, differs.
    :func:`~cuprum._group_teardown.signal_target_or_report` delivers to it and
    :func:`~cuprum._process_signal._settlement` waits on it, so those are the
    two places that decide which target this run has.

    Settled is decided by
    :func:`~cuprum._process_signal._members_remain_to_signal`, which owns the
    probe, the diagnostic and the veto. It and the
    :func:`~cuprum._process_signal._settlement_of` it consults state why a
    reaped child is not settlement for an owning run.

    *observation* is the execution this teardown is acting for, so a target
    that could not be reached is reported against the span and the metric of
    the run it belongs to. It defaults to ``None`` because several teardown
    routes have no execution to correlate with — the spawn-failure and
    abandoned-run paths reach here before any observation exists — and those
    keep the log record without inventing an identity for the event.

    Returns
    -------
    _TerminationOutcome
        What the stop did to the target. The group route adds a second way to
        deliver nothing: a probe that reports the group settled vetoes the
        signal before it is sent, and one that finds the group unsignalable
        leaves a refusal to report. Both are ``delivered=False`` without being
        a ``refused=True``, which only a raising delivery sets.
    """
    grace_period = max(0.0, policy.grace_period)
    owns_group = policy.owns_group_for(0)
    if not _members_remain_to_signal(
        process,
        target,
        observation=observation,
        owns_group=owns_group,
    ):
        return _NO_STOP
    if not signal_target_or_report(
        process,
        observation=observation,
        owns_group=owns_group,
        group_signal=signal.SIGTERM,
    ):
        return _NO_STOP
    # Past this point the graceful signal reached the child, whatever happens
    # to the waiter below.
    try:
        await asyncio.wait_for(
            _settlement(
                process,
                target,
                observation=observation,
                owns_group=owns_group,
            ),
            grace_period,
        )
    except asyncio.TimeoutError:  # ruff: ignore[timeout-error-alias] - explicit asyncio timeout needed
        return await _escalate_after_grace_outcome(
            process,
            target,
            observation=observation,
            owns_group=owns_group,
        )
    return _TERMINATED_IN_GRACE


async def _escalate_after_grace_outcome(
    process: asyncio.subprocess.Process,
    target: _SettlementTarget,
    *,
    observation: _StageObservation | None,
    owns_group: bool,
) -> _TerminationOutcome:
    """Compel a target that outlived its grace, and report that as an outcome.

    :func:`~cuprum._process_signal._escalate_after_grace` already answers the
    only question a signal can be refused over — whether the escalation was
    delivered — so this adds the two facts an outcome carries and that answer
    does not. The graceful signal that opened the window reached the child, and
    the window did run out; what remains is whether the escalation's own reap
    returned, which is exactly the distinction between the two forced endings
    an outcome names.

    Returns
    -------
    _TerminationOutcome
        ``_KILLED_AFTER_GRACE`` when the escalation was delivered and its reap
        returned, or ``_KILLED_AFTER_GRACE_UNWAITED`` when the signal could not
        be delivered, so no wait was ever started to return.
    """
    delivered = await _escalate_after_grace(
        process,
        target,
        observation=observation,
        owns_group=owns_group,
    )
    if not delivered:
        return _KILLED_AFTER_GRACE_UNWAITED
    return _KILLED_AFTER_GRACE


async def _settle_shielded[T](task: asyncio.Task[T]) -> T | None:
    """Wait out a shielded task after cancellation; report what it produced.

    Retrieving the outcome here keeps a task failure from surfacing later as an
    unretrieved task exception.

    Returns
    -------
    T | None
        What the task produced, or ``None`` when it produced nothing.
    """
    while not task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)
    if task.cancelled() or task.exception() is not None:
        return None
    return task.result()


async def _shielded_cleanup[T](cleanup: cabc.Awaitable[T]) -> T:
    """Complete cleanup before re-raising any caller cancellation."""
    task = asyncio.ensure_future(cleanup)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _settle_shielded(task)
        raise


class _ShieldedStepFailure[T](typ.NamedTuple):
    """How one shielded cleanup step ended, for the caller to weigh.

    A cancellation is reported separately from an ordinary failure because the
    two are owed different treatment: a cancellation must always reach the
    caller — it is a request that the task stop, not a failure to report — while
    an ordinary failure may legitimately defer to an error the caller is
    already raising. Reporting both in one value keeps that distinction visible
    to the caller instead of flattening it here.

    The value is carried alongside the failure rather than only returned on
    success, because a step's *outcome* and its *completion* are independent
    facts. A stop that delivered what it was asked to and then had its caller
    cancelled has still stopped the child, and a caller describing that stop
    needs to say so; a report that dropped the value whenever anything went
    wrong would leave it unable to.
    """

    cancellation: asyncio.CancelledError | None
    failure: BaseException | None
    value: T | None


async def _run_shielded_step[T](
    cleanup: cabc.Awaitable[T],
) -> _ShieldedStepFailure[T]:
    """Run a shielded cleanup, reporting rather than raising how it ended.

    The step is shielded exactly as ``_shielded_cleanup`` shields it, so a
    cancellation arriving mid-step still completes it. What differs is only
    what happens to the outcome: it is returned, because a caller running two
    steps has to weigh the first against the second before it can say which
    failure, if any, the caller is owed. Absorbing the failure here would
    decide that too early; raising it here would abandon the second step.

    Returns
    -------
    _ShieldedStepFailure
        How the step ended — the cancellation it absorbed, the failure it
        raised, or neither — together with whatever the step produced. The
        value is absent only when the step failed or was cancelled.
    """
    task = asyncio.ensure_future(cleanup)
    try:
        value = await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        # The step's own value survives the cancellation rather than being
        # dropped: a stop that delivered its signal and was cancelled while the
        # delivery was finishing has still stopped the child, and the control
        # event describing it has to be emitted.
        return _ShieldedStepFailure(cancelled, None, await _settle_shielded(task))
    except BaseException as failed:  # ruff: ignore[blind-except] - reported, not handled: the caller weighs it against a later step's failure
        return _ShieldedStepFailure(None, failed, None)
    return _ShieldedStepFailure(None, None, value)


async def _await_teardown_shielded(
    teardowns: cabc.Iterable[cabc.Awaitable[object]],
) -> tuple[object, ...]:
    """Complete teardown and return its outcomes after caller cancellation."""
    outcomes = await _shielded_cleanup(
        asyncio.gather(*teardowns, return_exceptions=True)
    )
    return tuple(outcomes)


async def _terminate_all_shielded(
    processes: cabc.Iterable[asyncio.subprocess.Process],
    policy: _TeardownPolicy,
    *,
    observations: cabc.Sequence[_StageObservation] = (),
) -> None:
    """Terminate every process before re-raising caller cancellation.

    *policy* carries the per-process ownership flags, so each process is
    signalled the way it was spawned. A policy whose sequence is shorter than
    ``processes`` leaves the rest on the direct-child route.

    *observations* carries the executions these processes belong to, in the
    same order, so each termination is reported against the execution whose
    process it signalled. Pairing is by index and the sequence is not passed
    on whole: a run-level report drawn from the front of the sequence would
    attribute every stage's refusal to the first execution, which is a
    different run from the one that was signalled. The default is empty for
    the routes with no execution to correlate with; those keep the log record
    alone.
    """
    await _await_teardown_shielded(
        _terminate_process(
            process,
            policy.for_index(index),
            observation=observations[index] if index < len(observations) else None,
        )
        for index, process in enumerate(processes)
    )
