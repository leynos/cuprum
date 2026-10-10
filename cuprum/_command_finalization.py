"""Driving a spawned command to completion and settling its observation.

Split from ``cuprum._command_internals`` when the stdio-resolution fix pushed
that module past the 400-line ceiling. The seam is the one the code already
drew: ``_command_internals`` decides *what* a run is — its observation, its
stdio plan, its resolved inputs — while this module owns everything that
happens once the child exists: driving it, invoking the after-hooks, and
settling the sink, the terminal event, and the observer tasks on every exit
path.

The ordering in the finalizers is the point of them. Closing the sink before
draining means a drain failure cannot replace a timeout annotation; draining
through ``_shielded_cleanup`` means a caller who cancels repeatedly is still
waited on rather than leaving observer tasks stranded. The success and failure
paths differ only in which terminal event they emit, so each spells the
ordering out itself rather than sharing a helper that would have to re-derive
which of the two it was.
"""

from __future__ import annotations

import typing as typ

from cuprum._observability import (
    _drain_tasks_during_cleanup,
    _wait_for_exec_hook_tasks,
)
from cuprum._pipeline_types import _EventDetails, _StageObservation
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._sink_lifecycle import _outcome_for_error, _outcome_for_result
from cuprum._subprocess_execution import (
    _execute_subprocess,
    _SubprocessExecution,
)
from cuprum._timeout_reporting import _safe_emit_terminal
from cuprum.events import TerminalOutcome

if typ.TYPE_CHECKING:
    from cuprum._execution_tracking import _ExecutionTracking
    from cuprum.sh import CommandResult, SafeCmd

__all__ = [
    "_execute_with_hooks",
    "_finalize_prepared_command_failure",
]

# Names the aggregate raised when draining observe-hook tasks fails while a
# single-command execution is already unwinding.
_COMMAND_FINALIZATION_ERROR = "command finalization failed"


async def _execute_with_hooks(
    cmd: SafeCmd,
    execution: _SubprocessExecution,
    tracking: _ExecutionTracking,
) -> CommandResult:
    """Run the subprocess and hooks, then settle after all observers finish.

    Cleanup aggregates observer failures with the active error; success-path
    hook failures remain direct. Shielding waits through repeated cancellation.

    Returns
    -------
    CommandResult
        The completed command's result after every observe-hook task drains.
    """
    result: CommandResult | None = None
    try:
        result = await _execute_subprocess(execution)
        for hook in tracking.execution_hooks.after_hooks:
            hook(cmd, result)
    except BaseException as run_error:
        await _finalize_command_run_failure(
            execution,
            tracking,
            result,
            run_error,
        )
        raise

    await _finalize_command_run_success(execution, tracking, result)
    return result


async def _finalize_command_run_failure(
    execution: _SubprocessExecution,
    tracking: _ExecutionTracking,
    result: CommandResult | None,
    run_error: BaseException,
) -> None:
    """Settle and drain an observed command after execution or hook failure."""
    # Close before the drain: its failure is grouped with the primary error.
    outcome = _outcome_for_error(run_error)
    tracking.sink_bracket.close(outcome=outcome)
    _safe_emit_terminal(
        execution.observation,
        outcome.outcome,
        _failed_command_details(
            execution.observation.started_pid,
            outcome.outcome,
            result,
        ),
    )
    await _shielded_cleanup(
        _drain_tasks_during_cleanup(
            tracking.pending_tasks,
            run_error,
            message=_COMMAND_FINALIZATION_ERROR,
        )
    )


def _failed_command_details(
    started_pid: int | None,
    outcome: TerminalOutcome,
    result: CommandResult | None,
) -> _EventDetails:
    """Retain only child details that remain valid after command failure."""
    duration_s = None if result is None else result.duration
    if outcome is TerminalOutcome.CANCELLED:
        return _EventDetails(pid=None, duration_s=duration_s)
    return _EventDetails(
        pid=started_pid if result is None else result.pid,
        exit_code=None if result is None else result.exit_code,
        duration_s=duration_s,
    )


async def _finalize_command_run_success(
    execution: _SubprocessExecution,
    tracking: _ExecutionTracking,
    result: CommandResult,
) -> None:
    """Settle a completed command, then drain every observe-hook task."""
    outcome = _outcome_for_result(result)
    tracking.sink_bracket.close(outcome=outcome)
    try:
        execution.observation.emit_terminal(
            outcome.outcome,
            _EventDetails(
                pid=result.pid,
                exit_code=result.exit_code,
                duration_s=result.duration,
            ),
        )
    except BaseException as terminal_error:
        await _shielded_cleanup(
            _drain_tasks_during_cleanup(
                tracking.pending_tasks,
                terminal_error,
                message=_COMMAND_FINALIZATION_ERROR,
            )
        )
        raise
    await _shielded_cleanup(_wait_for_exec_hook_tasks(tracking.pending_tasks))


async def _finalize_prepared_command_failure(
    tracking: _ExecutionTracking,
    observation: _StageObservation | None,
    run_error: BaseException,
) -> None:
    """Close, settle, and drain a command that failed before ownership passed on."""
    # Close before the drain: a hook failure is grouped with the run error, and
    # closing afterwards would record that aggregate instead of a timeout.
    outcome = _outcome_for_error(run_error)
    tracking.sink_bracket.close(outcome=outcome)
    if observation is not None:
        _safe_emit_terminal(
            observation,
            outcome.outcome,
            _EventDetails(
                pid=(
                    None
                    if outcome.outcome is TerminalOutcome.CANCELLED
                    else observation.started_pid
                ),
            ),
        )
    # A plan observer or before-hook can schedule tasks before execution starts.
    # This layer still owns them, so it must drain them before re-raising.
    await _shielded_cleanup(
        _drain_tasks_during_cleanup(
            tracking.pending_tasks,
            run_error,
            message=_COMMAND_FINALIZATION_ERROR,
        )
    )
