"""Private execution orchestration for ``SafeCmd.run`` and ``run_sync``.

Like :mod:`cuprum._pipeline_internals`, this module prepares a validated
command's observation, drives its subprocess execution, and finalizes the
presentation-sink session. Public command types and pipeline orchestration
remain in :mod:`cuprum.sh`.

On failure, finalization closes the sink before draining observer tasks so a
drain error cannot replace a timeout annotation. Drains use
:func:`cuprum._process_lifecycle._shielded_cleanup`, which waits through
repeated caller cancellation.
"""

from __future__ import annotations

import dataclasses as dc
import time
import typing as typ

from cuprum._command_execution_config import _build_subprocess_execution
from cuprum._delivery_lifecycle import _close_delivery_worker
from cuprum._execution_tracking import _ExecutionTracking
from cuprum._observability import (
    _base_stage_tags,
    _drain_tasks_during_cleanup,
    _merge_tags,
    _resolve_env_overlay,
    _resolve_executable_for,
    _wait_for_exec_hook_tasks,
    _without_env_mode_tag,
)
from cuprum._pipeline_types import (
    _EventDetails,
    _StageObservation,
)
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._sink_lifecycle import (
    _outcome_for_error,
    _outcome_for_result,
)
from cuprum._subprocess_context import _event_cwd, _execution_cwd
from cuprum._subprocess_execution import (
    _execute_subprocess,
    _SubprocessExecution,
)
from cuprum._timeout_reporting import _safe_emit_terminal
from cuprum.context import EnvMode
from cuprum.events import CaptureMode, TerminalOutcome

if typ.TYPE_CHECKING:
    from cuprum._result_types import _AnyCommandResult
    from cuprum.sh import ExecutionContext, RunOutputOptions, SafeCmd

__all__ = [
    "_ExecutionState",
    "_ExecutionTracking",
    "_build_subprocess_execution",
    "_execute_with_hooks",
    "_finalize_prepared_command_failure",
    "_prepare_execution_observation",
    "_run_prepared_command",
]

# Names the aggregate raised when draining observe-hook tasks fails while a
# single-command execution is already unwinding.
_COMMAND_FINALIZATION_ERROR = "command finalization failed"


@dc.dataclass(frozen=True, slots=True)
class _ExecutionState:
    """One run's already-resolved inputs, carried as a unit.

    Every field is resolved before the sink session opens — the allowlist is
    enforced, stdin is resolved against the context, and the timeout
    precedence is settled — so the bundle changes nothing about when those
    steps happen, only how many names the run's orchestration helpers have to
    take.

    The entries that start a run each build one and hand it on: ``SafeCmd``
    for the foreground, background, and owned-handle paths, and
    ``_spawn_owned_run`` for the handle's own start. Nothing in this module
    constructs it, which is why the orchestrators here take it by reference
    and never assemble a partial one.
    """

    context: ExecutionContext
    output: RunOutputOptions
    stdin_data: bytes | None
    timeout: float | None
    # Whether captures are reported as bytes rather than decoded text, carried
    # to both the drain and the event's ``capture_mode``.
    capture_bytes: bool = False


def _prepare_execution_observation(
    cmd: SafeCmd,
    state: _ExecutionState,
    tracking: _ExecutionTracking,
) -> _StageObservation:
    """Prepare the observation context for command execution."""
    context = state.context
    output = state.output
    cwd = _event_cwd(context.cwd)
    env_overlay, env_mode = _resolve_env_overlay(context.env, context.env_mode)
    # Resolved here, after the caller's allowlist check, so the resolver runs
    # once for a command that is actually going to run.
    resolved_path = _resolve_executable_for(cmd, cwd=_execution_cwd(context.cwd))
    tags = _merge_tags(
        _base_stage_tags(
            cmd,
            capture=output.capture,
            echo_stdout=output.resolved_echo[0],
            echo_stderr=output.resolved_echo[1],
        ),
        _without_env_mode_tag(context.tags),
        {"env_mode": env_mode} if env_mode is EnvMode.REPLACE else None,
    )
    return _StageObservation(
        cmd=cmd,
        hooks=tracking.execution_hooks,
        cwd=cwd,
        env_overlay=env_overlay,
        tags=tags,
        pending_tasks=tracking.pending_tasks,
        # Resolved here, not at the class-creation field default a patch misses.
        wall_clock=time.time,
        monotonic_clock=time.perf_counter,
        env_mode=env_mode,
        resolved_path=resolved_path,
        capture_mode=CaptureMode.BYTES if state.capture_bytes else CaptureMode.TEXT,
    )


async def _execute_with_hooks(
    cmd: SafeCmd,
    execution: _SubprocessExecution,
    tracking: _ExecutionTracking,
) -> _AnyCommandResult:
    """Run the subprocess and hooks, then settle after all observers finish.

    Cleanup aggregates observer failures with the active error; success-path
    hook failures remain direct. Shielding waits through repeated cancellation.

    Returns
    -------
    CommandResult | BytesCommandResult
        The completed command's result after every observe-hook task drains.
    """
    result: _AnyCommandResult | None = None
    try:
        result = await _execute_subprocess(execution)
        await _shielded_cleanup(
            _close_delivery_worker(
                execution.delivery_worker,
                drain_grace_s=execution.delivery_grace_s,
            ),
        )
        if execution.relay_diagnostics is not None:
            stdout_diagnostics, stderr_diagnostics = execution.relay_diagnostics
            result = dc.replace(
                result,
                relay_fallbacks=(
                    stdout_diagnostics.snapshot() + stderr_diagnostics.snapshot()
                ),
            )
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
    result: _AnyCommandResult | None,
    run_error: BaseException,
) -> None:
    """Settle and drain an observed command after execution or hook failure."""
    # Close before the drain: its failure is grouped with the primary error.
    outcome = _outcome_for_error(run_error)
    await _shielded_cleanup(
        _close_delivery_worker(
            execution.delivery_worker,
            drain_grace_s=execution.delivery_grace_s,
        ),
    )
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
    result: _AnyCommandResult | None,
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
    result: _AnyCommandResult,
) -> None:
    """Settle a completed command, then drain every observe-hook task."""
    outcome = _outcome_for_result(result)
    await _shielded_cleanup(
        _close_delivery_worker(
            execution.delivery_worker,
            drain_grace_s=execution.delivery_grace_s,
        ),
    )
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


async def _run_prepared_command(
    cmd: SafeCmd,
    state: _ExecutionState,
) -> _AnyCommandResult:
    """Run one validated command after its public inputs are resolved.

    ``SafeCmd.run`` remains the public entry point and keeps its signature; it
    resolves the allowlist, stdin, and the effective timeout, bundles them as
    *state*, and hands the bundle here. This helper takes that bundle instead
    of the four names it carries, which keeps the orchestration signature
    within CodeScene's argument limit and makes the bundle what the docstring
    on :class:`_ExecutionState` claims it is — one run's resolved inputs
    travelling as a unit — rather than a temporary built only to be unpacked.

    The preparation prefix is shared with ``SafeCmd.start()`` and lives in
    ``cuprum._command_prepare``, so the two entry points cannot drift apart.
    It owns the failure handling for everything it does; what remains here is
    executed under :func:`_execute_with_hooks`, which owns finalization for the
    run itself.

    Parameters
    ----------
    cmd : SafeCmd
        The validated command to run.
    state : _ExecutionState
        The run's already-resolved inputs.

    Returns
    -------
    CommandResult | BytesCommandResult
        The completed command's result, byte-exact when bytes were asked for.
    """
    # Imported here, not at module scope: ``cuprum._command_prepare`` borrows
    # helpers from this module, so a module-level import would close the cycle.
    from cuprum._command_prepare import _prepare_owned_command

    prepared = await _prepare_owned_command(cmd, state)
    return await _execute_with_hooks(
        cmd,
        prepared.execution,
        prepared.tracking,
    )
