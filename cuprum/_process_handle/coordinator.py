"""Waiting, settling, and result assembly for an owned running command.

One run reaches its end by several routes — the child exits on its own, a
deadline expires, the caller terminates it, the caller kills it, the enclosing
``async with`` block ends, or a cancellation arrives mid-teardown — and every
one of them has to produce the same three things: the child reaped, its stream
tasks reconciled exactly once, and a ``CommandResult`` reporting how it
actually ended. That convergence is this module's whole job.

There is deliberately one assembly path. ``terminate()`` and ``kill()`` do not
build a result of their own; they stop the child, and the ordinary wait that
follows observes the death and reports the signal that caused it. A second
assembly path would be a second place for the exit code, the exit event, and
the relay diagnostics to be read at different moments.

The exit event and the result are built together, so a consumer reading the
observation stream sees the exit code and the resource figures the caller's
result carries instead of having to correlate two records. Only the child's
own ``wait4`` measurement is reported, and holding the handle does not change
it: that figure is attributed to the PID by the kernel whenever the wait
began, so it describes the child rather than a window this path chose. The
aggregate ``RUSAGE_CHILDREN`` delta is deliberately *not* reported here, and
the reason is the one the old comment got right about a different case: it is
a window measurement, this path takes no before-spawn snapshot, and a figure
measured late would describe a different window than the one it claims.
Leaving that variant unmeasured is honest; measuring it late is not.

A deadline is applied by the caller, not here. The wait under this module is
unbounded — the child's end is the only thing that stops it — and
:mod:`cuprum._process_handle.settlement` is where a caller's own clock is
turned into a stop and an expiry. Keeping the two apart is what stops one
caller's deadline from being imposed on another's wait.

Reconciliation always runs shielded, and always *after* the process is gone.
The order matters. Cancelling a stream consumer while the child is still
writing would turn a clean shutdown into a broken pipe, and a cancellation
landing mid-drain would leave consumers nobody holds.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum._delivery_lifecycle import _close_delivery_worker
from cuprum._process_handle.results import (
    _assemble_handle_result,
    _handle_outcome,
)
from cuprum._process_handle.tasks import _HandleRun, _launch_idle_monitor
from cuprum._process_lifecycle import _shielded_cleanup, _terminate_process
from cuprum._subprocess_wait import (
    _drain_context,
    _reconcile_run_tasks,
    _wait_for_exit_code_within_timeout,
)
from cuprum._teardown_policy import _TeardownPolicy

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._subprocess_spawn import _SubprocessExecution
    from cuprum._subprocess_wait_types import _DrainPair
    from cuprum.sh import CommandResult

__all__ = [
    "_ReconcileHooks",
    "_close_handle_delivery",
    "_finalize_settled_run",
    "_settle_handle_run",
]


async def _settle_owned_group_before_draining(run: _HandleRun) -> None:
    """Terminate the run's owned group so the drain that follows can reach EOF.

    A handle on a child it does not own has nothing to do here: its only
    teardown target is the direct child, which has already exited on every
    path that reaches this helper, so the call is skipped rather than made a
    no-op.

    An owning handle is a different question, and the same one the streamed
    run answers. The direct child has exited, but the group it led may not
    have: a descendant it left behind is still a member, still holds the write
    end of the run's inherited pipes, and no signal aimed at the leader can
    reach it. Draining first would spend the capture grace on readers that
    cannot reach end-of-file and then discard output the descendant was still
    producing.

    Ownership is read from the bundle rather than taken as a parameter, so
    this decision and the spawn's are read from one field and cannot fall out
    of step.
    """
    if not run.execution.owns_process_group:
        return
    await _terminate_process(
        run.process,
        _TeardownPolicy(
            run.execution.ctx.cancel_grace,
            owns_group=True,
        ),
        observation=run.execution.observation,
    )


async def _close_handle_delivery(execution: _SubprocessExecution) -> None:
    """Close the run's delivery worker before its result is built.

    Handle results and after-hooks must see write failures and undelivered
    bytes discovered only when the shared delivery worker settles, so the
    worker closes before the outcome is built. Both handle modes need exactly
    this step, so it is stated once.
    """
    await _shielded_cleanup(
        _close_delivery_worker(
            execution.delivery_worker,
            drain_grace_s=execution.delivery_grace_s,
        ),
    )


@dc.dataclass(frozen=True, slots=True)
class _ReconcileHooks:
    """Optional work a run wraps around its stream reconciliation.

    A byte run releases its live queues before the drain and its output
    transports after it. Bundling the two hooks keeps the coordinator's
    signature within the repository's argument limit and makes the pair a
    property of the run rather than two independent parameters a caller could
    supply one of.
    """

    before: cabc.Callable[[], None] | None = None
    after: cabc.Callable[[], cabc.Awaitable[None]] | None = None


_NO_RECONCILE_HOOKS = _ReconcileHooks()


async def _finalize_settled_run(
    run: _HandleRun,
    execution: _SubprocessExecution,
    *,
    preserve: bool,
    hooks: _ReconcileHooks = _NO_RECONCILE_HOOKS,
) -> _DrainPair:
    """Reconcile an ended run's tasks, keeping its output if asked.

    An owned group is settled first, because the drain below is what it would
    otherwise block: ``_reconcile_run_tasks`` stops the watchdog, cancels the
    stdin writer, drains the consumers, and settles the relay diagnostics —
    and a descendant holding the pipes open is what stops those consumers
    reaching end-of-file. The sequence is otherwise the one every exit path in
    the library uses.

    ``hooks.before`` runs after the group is settled and immediately before
    the drain. A byte run uses it to release its live queues, so a consumer
    that stopped reading cannot hold the reconciliation open. ``hooks.after``
    runs once the drain has settled, on both its success and failure paths, so
    a byte run can release output transports no reader will consume again.

    Both phases run under **one** shield: ``_shielded_cleanup`` finishes its
    task and then re-raises the caller's cancellation, so two sequential
    shielded calls would not be one shielded sequence — a cancellation during
    the first would exit before the second was awaited. Composing them into
    one coroutine makes the shield cover the pair.

    The drain is what makes the captured output available to whoever reports
    the run's end — the settled result, or the expiry a caller's deadline is
    owed — and it happens exactly once per run.

    Parameters
    ----------
    run : _HandleRun
        The owned run whose tasks are being reconciled.
    execution : _SubprocessExecution
        The bundle the wait was driven with, which carries the observation the
        drain reports through.
    preserve : bool
        Whether to keep the drained output. False while another error is
        propagating, so a drain failure cannot mask it.
    hooks : _ReconcileHooks, default=_NO_RECONCILE_HOOKS
        The optional synchronous hook run just before the consumers settle and
        the optional asynchronous hook run inside the same shield once the
        drain ends.

    Returns
    -------
    _DrainPair
        The drained stdout and stderr, each a payload with the record
        describing how its policy retained it.
    """

    async def settle_then_reconcile() -> _DrainPair:
        """Settle the owned group, then reconcile the run's tasks, as one unit."""
        await _settle_owned_group_before_draining(run)
        if hooks.before is not None:
            hooks.before()
        try:
            return await _reconcile_run_tasks(
                run.ownership,
                _drain_context(
                    execution,
                    run.ownership,
                    run.process.pid,
                    preserve=preserve,
                ),
            )
        finally:
            if hooks.after is not None:
                await hooks.after()

    return await _shielded_cleanup(settle_then_reconcile())


async def _settle_handle_run(run: _HandleRun) -> CommandResult:
    """Wait for the child, reconcile its tasks, and assemble the outcome.

    The watchdog is armed here rather than at spawn, so the time a caller
    spends holding a handle without waiting is not counted as the child's
    silence; arming is idempotent, so a later stop is unaffected.

    The wait is unbounded. A child that was stopped before this ran is already
    gone, so the wait returns at once and the exit code it reports is the
    signal that ended the child — ``-15`` for a cooperative ``SIGTERM``,
    ``-9`` for one that had to be killed — rather than whatever a stop path
    happened to guess.

    Returns
    -------
    CommandResult
        The command's outcome, with its streams drained exactly once.

    Raises
    ------
    asyncio.CancelledError
        If the settlement task was cancelled while waiting, once the run is
        reconciled.
    """  # ruff: ignore[docstring-extraneous-exception] - CancelledError propagates from the wait
    execution = run.execution
    _launch_idle_monitor(execution)
    try:
        exit_code, exited_at = await _wait_for_exit_code_within_timeout(
            run.process,
            execution,
        )
    except BaseException:
        # Cancellation, and any other failure escaping the wait, still needs
        # the run reconciled before it propagates. No output is kept while
        # another error is on its way out.
        await _finalize_settled_run(run, execution, preserve=False)
        raise
    drained = await _finalize_settled_run(
        run,
        execution,
        preserve=True,
    )
    await _close_handle_delivery(execution)
    return _assemble_handle_result(
        run,
        _handle_outcome(exit_code, exited_at, drained),
        capture_bytes=False,
    )
