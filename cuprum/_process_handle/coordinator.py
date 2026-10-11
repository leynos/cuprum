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
observation stream sees the exit code the caller's result carries instead of
having to correlate two records. The exit event deliberately reports no
resource usage: the wait here can begin after the child has already been
reaped, so a figure measured then would describe a different window than the
one it claims. Leaving it unmeasured is honest; measuring it late is not.

A deadline is the one route where the caller's error and the child's outcome
diverge, so it reports both. The wait that expires stops the child exactly as a
cancelled ``run()`` does and the child is gone by the time the drain runs, so
there is a real outcome to publish — but the caller whose deadline elapsed is
still owed a ``TimeoutExpired``. :class:`_HandleSettlement` carries the pair,
which is what lets the handle record the outcome before it raises.

Reconciliation always runs shielded, and always *after* the process is gone.
The order matters. Cancelling a stream consumer while the child is still
writing would turn a clean shutdown into a broken pipe, and a cancellation
landing mid-drain would leave consumers nobody holds.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from time import perf_counter

from cuprum._process_handle.tasks import _HandleRun, _launch_idle_monitor
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._subprocess_context import _sh_module
from cuprum._subprocess_execution import _relay_fallbacks_for_result
from cuprum._subprocess_timeout import (
    _emit_exit_event,
    _ExitEventDetails,
    _get_exit_code,
    _resolve_timeout_payload,
    _SubprocessTimeoutError,
    _TimeoutFallback,
)
from cuprum._subprocess_wait import (
    _DrainContext,
    _reconcile_run_tasks,
    _wait_for_exit_code_within_timeout,
)

if typ.TYPE_CHECKING:
    from cuprum._subprocess_spawn import _SubprocessExecution
    from cuprum.sh import CommandResult, TimeoutExpired

__all__ = [
    "_finalize_settled_run",
    "_settle_handle_run",
]


async def _finalize_settled_run(
    run: _HandleRun,
    execution: _SubprocessExecution,
    *,
    capture: bool,
) -> tuple[str | None, str | None]:
    """Reconcile an ended run's tasks, keeping its output if asked.

    ``_reconcile_run_tasks`` stops the watchdog, cancels the stdin writer,
    drains the consumers, and settles the relay diagnostics — the sequence
    every exit path in the library uses. It runs shielded so a cancellation
    arriving now cannot strand half the tasks, and it is idempotent, so the
    several routes into it do not have to coordinate.

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
    return await _shielded_cleanup(
        _reconcile_run_tasks(
            run.ownership,
            _DrainContext(
                capture=capture,
                pid=run.process.pid,
                observation=execution.observation,
                discard_on_cancel=run.ownership.discard_on_cancel,
            ),
        ),
    )


async def _settle_handle_run(
    run: _HandleRun,
    execution: _SubprocessExecution,
) -> _HandleSettlement:
    """Wait for the child, reconcile its tasks, and assemble the outcome.

    The watchdog is armed here rather than at spawn, so the time a caller
    spends holding a handle without waiting is not counted as the child's
    silence; arming is idempotent, so a later stop is unaffected.

    A child that was stopped before this ran is already gone, so the wait
    returns at once and the exit code it reports is the signal that ended the
    child — ``-15`` for a cooperative ``SIGTERM``, ``-9`` for one that had to
    be killed — rather than whatever a stop path happened to guess.

    Returns
    -------
    _HandleSettlement
        The command's outcome, and the deadline error to raise in place of
        offering it when a deadline is what ended the wait.

    Raises
    ------
    asyncio.CancelledError
        If the caller cancelled while waiting, once the run is reconciled.
    """  # ruff: ignore[docstring-extraneous-exception] - CancelledError propagates from the wait
    _launch_idle_monitor(execution)
    try:
        exit_code, exited_at = await _wait_for_exit_code_within_timeout(
            run.process,
            execution,
        )
    except (TimeoutError, _SubprocessTimeoutError) as exc:
        stdout_text, stderr_text = await _finalize_settled_run(
            run,
            execution,
            capture=True,
        )
        # The child is stopped and drained by this point, so its outcome is a
        # settled fact worth keeping. It is recorded beside the error the
        # caller's deadline earned rather than instead of it.
        return _HandleSettlement(
            result=_build_handle_result(
                run,
                _HandleOutcome(
                    exit_code=_get_exit_code(run.process),
                    # The exit was never observed here, so the reading taken
                    # now is the best available one — the same value the
                    # streamed path uses as its fallback.
                    exited_at=perf_counter(),
                    stdout_text=stdout_text,
                    stderr_text=stderr_text,
                ),
            ),
            deadline_error=_deadline_error(execution, exc),
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
    return _HandleSettlement(
        result=_build_handle_result(
            run,
            _HandleOutcome(
                exit_code=exit_code,
                exited_at=exited_at,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
            ),
        ),
        deadline_error=None,
    )


@dc.dataclass(frozen=True, slots=True)
class _HandleSettlement:
    """What one settlement produced: the outcome, and why a wait did not get it.

    The two travel together because a deadline is the one route where both
    exist. Every other route to the end of a run — the child exiting, a stop,
    a kill, a teardown — has an outcome and nothing to raise, so the handle can
    offer the result straight back. A deadline has an outcome *and* an error:
    the child was stopped and reaped, and the caller whose clock ran out is
    owed the error rather than the outcome.

    ``deadline_error`` is deliberately built here rather than raised here, so
    the handle can record the outcome before it raises. Raising from this
    module would discard it, because the settlement task's own exception is
    what every later operation would then re-raise.
    """

    result: CommandResult
    deadline_error: TimeoutExpired | None

    @property
    def has_deadline_expired(self) -> bool:
        """Whether the settlement ended because a wait's deadline elapsed."""
        return self.deadline_error is not None


def _deadline_error(
    execution: _SubprocessExecution,
    exc: TimeoutError | _SubprocessTimeoutError,
) -> TimeoutExpired:
    """Build the ``TimeoutExpired`` the streamed path would have raised.

    The resolution is delegated to the same seam the streamed path uses, so a
    timeout reports the same command, the same configured timeout, and the same
    captured output whichever entry point hit it. Only the raising is left to
    the handle, which needs the outcome this error would otherwise displace.

    Returns
    -------
    TimeoutExpired
        The report of the expiry, chained onto the internal timeout that
        carried the captured output.
    """
    details = _resolve_timeout_payload(
        exc,
        _TimeoutFallback(
            configured_timeout=execution.timeout,
            stdout=None,
            stderr=None,
            exited_at=perf_counter(),
        ),
    )
    return _sh_module().TimeoutExpired(
        cmd=execution.cmd.argv_with_program,
        timeout=details.timeout,
        output=details.stdout,
        stderr=details.stderr,
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
        and the relay-diagnostic fields the run recorded.
    """
    execution = run.execution
    _emit_exit_event(
        execution.observation,
        _ExitEventDetails(
            pid=run.process.pid,
            exit_code=outcome.exit_code,
            started_at=run.started_at,
            exited_at=outcome.exited_at,
            # Deliberately left unmeasured. On this path the wait can begin
            # after the child was already reaped, so any figure read now would
            # describe a different window than the one it claims. The event
            # says ``unavailable`` rather than reporting zeros that would read
            # as a child that consumed nothing.
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
        relay_fallbacks=_relay_fallbacks_for_result(run.ownership.relay_diagnostics),
    )
