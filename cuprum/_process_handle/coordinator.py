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

from cuprum import _wait4_spawn
from cuprum._process_handle.tasks import _HandleRun, _launch_idle_monitor
from cuprum._process_lifecycle import _shielded_cleanup, _terminate_process
from cuprum._result_assembly import _require_text
from cuprum._subprocess_context import _sh_module
from cuprum._subprocess_execution import _relay_fallbacks_for_result
from cuprum._subprocess_timeout import _emit_exit_event, _ExitEventDetails
from cuprum._subprocess_wait import (
    _DrainContext,
    _reconcile_run_tasks,
    _wait_for_exit_code_within_timeout,
)
from cuprum._teardown_policy import _TeardownPolicy

if typ.TYPE_CHECKING:
    from cuprum._subprocess_spawn import _SubprocessExecution
    from cuprum._subprocess_wait_types import _StreamPayloadPair
    from cuprum.sh import CommandResult

__all__ = [
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


async def _finalize_settled_run(
    run: _HandleRun,
    execution: _SubprocessExecution,
    *,
    capture: bool,
) -> tuple[str | None, str | None]:
    """Reconcile an ended run's tasks, keeping its output if asked.

    An owned group is settled first, because the drain below is what it would
    otherwise block: ``_reconcile_run_tasks`` stops the watchdog, cancels the
    stdin writer, drains the consumers, and settles the relay diagnostics —
    and a descendant holding the pipes open is what stops those consumers
    reaching end-of-file. The sequence is otherwise the one every exit path in
    the library uses.

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
    capture : bool
        Whether to keep the drained output. False while another error is
        propagating, so a drain failure cannot mask it.

    Returns
    -------
    tuple[str | None, str | None]
        The decoded stdout and stderr; both ``None`` when *capture* is false.
    """

    async def settle_then_reconcile() -> _StreamPayloadPair:
        """Settle the owned group, then reconcile the run's tasks, as one unit."""
        await _settle_owned_group_before_draining(run)
        return await _reconcile_run_tasks(
            run.ownership,
            _DrainContext(
                capture=capture,
                pid=run.process.pid,
                observation=execution.observation,
                discard_on_cancel=run.ownership.discard_on_cancel,
            ),
        )

    drained = await _shielded_cleanup(settle_then_reconcile())
    # A handle always runs in text mode: ``start()`` forces capture on and never
    # offers the byte-exact entry points, so the drain's widened payload is
    # text here. Narrowing through ``_require_text`` re-establishes that
    # guarantee and reports a contradiction rather than asserting it away.
    return _require_text(drained[0], "stdout"), _require_text(drained[1], "stderr")


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
        await _finalize_settled_run(run, execution, capture=False)
        raise
    stdout_text, stderr_text = await _finalize_settled_run(
        run,
        execution,
        capture=True,
    )
    return _build_handle_result(
        run,
        _HandleOutcome(
            exit_code=exit_code,
            exited_at=exited_at,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
        ),
    )


@dc.dataclass(frozen=True, slots=True)
class _HandleOutcome:
    """The settled facts of an owned run, ready to be published.

    The exit code and the drained text are produced by whichever route reached
    the end of the wait, so they travel as one record rather than as four
    arguments apiece. That keeps the publishing helper's signature fixed no
    matter which route reached it, and keeps every route's ``CommandResult``
    carrying the same fields.
    """

    exit_code: int
    exited_at: float
    stdout_text: str | None
    stderr_text: str | None


def _build_handle_result(run: _HandleRun, outcome: _HandleOutcome) -> CommandResult:
    """Publish the run's exit event and assemble its ``CommandResult``.

    The two are built together so the event a consumer observes carries the
    same exit code the caller's result does. The result's ``duration`` is
    measured from the spawn, so it spans the time the handle was held — the
    child's lifetime, not the caller's wait.

    Returns
    -------
    CommandResult
        The command's outcome, carrying the captured streams, the exit code,
        the resource measurements the child's own reap produced, and the
        relay-diagnostic fields the run recorded.
    """
    execution = run.execution
    # The ``None`` before-snapshot is deliberate rather than an omission: it
    # selects the child's own ``wait4`` figure, which the kernel attributes to
    # this PID whenever the wait began, and it makes the aggregate
    # ``RUSAGE_CHILDREN`` delta unreachable — that variant needs a before-spawn
    # snapshot this path has no honest way to take, so it yields ``None``
    # instead of a figure measuring a window that would not be the child's.
    # Reporting nothing at all was the wrong default for the backend that can
    # answer: it left ``max_rss_bytes`` unavailable on a handle result while
    # ``run()`` reported it for the same command.
    rusage = _wait4_spawn.resource_usage_for(run.process, None)
    _emit_exit_event(
        execution.observation,
        _ExitEventDetails(
            pid=run.process.pid,
            exit_code=outcome.exit_code,
            started_at=run.started_at,
            exited_at=outcome.exited_at,
            resource_usage=rusage,
        ),
    )
    return _sh_module().CommandResult(
        program=execution.cmd.program,
        argv=execution.cmd.argv,
        exit_code=outcome.exit_code,
        pid=run.process.pid if run.process.pid is not None else -1,
        stdout=outcome.stdout_text,
        stderr=outcome.stderr_text,
        started_at=run.wall_clock_started_at,
        duration=max(0.0, outcome.exited_at - run.started_at),
        # The same figures the exit event above carries, so the caller's result
        # and the observed event agree rather than one having to be correlated
        # with the other.
        max_rss_bytes=None if rusage is None else rusage.max_rss_bytes,
        user_cpu_seconds=None if rusage is None else rusage.user_cpu_seconds,
        system_cpu_seconds=None if rusage is None else rusage.system_cpu_seconds,
        # Carried through from the observation for the same reason ``run()``
        # carries it: the handle spawns the bound executable, so a result that
        # reported ``None`` here would claim no binding applied while the
        # caller's binding is exactly what chose the file that ran.
        resolved_path=execution.observation.resolved_path,
        relay_fallbacks=_relay_fallbacks_for_result(run.ownership.relay_diagnostics),
    )
