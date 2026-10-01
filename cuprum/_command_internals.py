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
import sys
import time
import typing as typ
from pathlib import Path

from cuprum._execution_tracking import _ExecutionTracking
from cuprum._idle_diagnostic import _idle_subject
from cuprum._idle_heartbeat import _build_idle_monitor
from cuprum._observability import (
    _base_stage_tags,
    _drain_tasks_during_cleanup,
    _merge_tags,
    _resolve_env_overlay,
    _wait_for_exec_hook_tasks,
    _without_env_mode_tag,
)
from cuprum._pipeline_internals import _collect_hooks
from cuprum._pipeline_types import (
    _EventDetails,
    _StageObservation,
)
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._sink_lifecycle import (
    _command_session_start,
    _outcome_for_error,
    _outcome_for_result,
    _SinkBracket,
)
from cuprum._subprocess_execution import (
    _execute_subprocess,
    _SubprocessExecution,
)
from cuprum._subprocess_streams import _resolve_stream_sink
from cuprum._timeout_reporting import _safe_emit_terminal
from cuprum.context import EnvMode, current_context
from cuprum.events import TerminalOutcome

if typ.TYPE_CHECKING:
    from cuprum.sh import (
        CommandResult,
        ExecutionContext,
        RunOutputOptions,
        SafeCmd,
    )
    from cuprum.sinks import base as sinks

__all__ = [
    "_ExecutionState",
    "_ExecutionTracking",
    "_build_subprocess_execution",
    "_execute_with_hooks",
    "_prepare_execution_observation",
    "_run_prepared_command",
]

# Names the aggregate raised when draining observe-hook tasks fails while a
# single-command execution is already unwinding.
_COMMAND_FINALIZATION_ERROR = "command finalization failed"


@dc.dataclass(frozen=True, slots=True)
class _ExecutionState:
    """One run's already-resolved inputs, carried as a unit.

    Every field is resolved by ``SafeCmd.run`` before the sink session opens —
    the allowlist is enforced, stdin is resolved against the context, and the
    timeout precedence is settled — so the bundle changes nothing about when
    those steps happen, only how many names the run's orchestration helpers
    have to take. ``SafeCmd.run`` builds it and hands it on; nothing here
    constructs it.
    """

    context: ExecutionContext
    output: RunOutputOptions
    stdin_data: bytes | None
    timeout: float | None
    # Whether the captured streams are reported as bytes rather than decoded
    # text. Defaulted rather than required because the text-mode entry point
    # is the one the whole codebase calls; the binary entry points are the
    # only callers that set it.
    capture_bytes: bool = False


def _prepare_execution_observation(
    cmd: SafeCmd,
    context: ExecutionContext,
    tracking: _ExecutionTracking,
    output: RunOutputOptions,
) -> _StageObservation:
    """Prepare the observation context for command execution."""
    cwd = Path(context.cwd) if context.cwd is not None else None
    env_overlay, env_mode = _resolve_env_overlay(context.env, context.env_mode)
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
        wall_clock=time.time,
        env_mode=env_mode,
    )


def _build_subprocess_execution(
    cmd: SafeCmd,
    state: _ExecutionState,
    *,
    observation: _StageObservation,
    sink_session: sinks.OutputSession | None = None,
) -> _SubprocessExecution:
    """Bundle everything one command's execution needs, before it spawns.

    The run's already-resolved inputs arrive together as *state*, so this
    helper stays a pure translation from what the run decided to what the
    subprocess layer consumes; *observation* and *sink_session* stay separate
    because they are the two products of the run's own preparation rather
    than inputs it was handed.

    The idle monitor is part of the bundle rather than an execution-time
    argument because its presence is what decides whether the child's stdout
    and stderr are piped for activity observation. Deferring it would leave
    the spawn unable to make that choice. The sink session travels with it
    for the same reason: stream wiring routes mirrored output through the
    session's log, so it has to be part of the bundle before the consumers
    are built.

    Returns
    -------
    _SubprocessExecution
        The resolved execution bundle, ready for ``_execute_with_hooks``.
    """
    return _SubprocessExecution(
        cmd=cmd,
        ctx=state.context,
        capture=state.output.capture,
        echo_stdout=state.output.resolved_echo[0],
        echo_stderr=state.output.resolved_echo[1],
        max_echo_line_bytes=state.output.max_echo_line_bytes,
        broken_pipe_policy=state.output.resolved_broken_pipe_policy,
        sink_session=sink_session,
        timeout=state.timeout,
        observation=observation,
        stdin_data=state.stdin_data,
        on_line=state.output.on_line,
        capture_bytes=state.capture_bytes,
        # Built here, during the parent's own preparation, but armed by the run
        # itself, once the child is actually running: everything that precedes
        # the spawn is the parent's work, and must not read as the child's
        # silence.
        idle=_build_idle_monitor(
            state.output.idle_after,
            state.output.on_idle,
            _idle_subject(str(cmd.program)),
            # Resolved the way the stderr drain resolves its own sink — the
            # same session, the same configured sink, the same last resort —
            # because the keepalive shares that stream's incomplete-line
            # state. An active session frames the mirrored streams, so a
            # keepalive written anywhere else would land outside the group the
            # run is claiming.
            _resolve_stream_sink(
                sink_session,
                state.context.stderr_sink,
                sys.stderr,
            ),
        ),
    )


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


async def _run_prepared_command(
    cmd: SafeCmd,
    state: _ExecutionState,
) -> CommandResult:
    """Run one validated command after its public inputs are resolved.

    ``SafeCmd.run`` remains the public entry point and keeps its signature; it
    resolves the allowlist, stdin, and the effective timeout, bundles them as
    *state*, and hands the bundle here. This helper takes that bundle instead
    of the four names it carries, which keeps the orchestration signature
    within CodeScene's argument limit and makes the bundle what the docstring
    on :class:`_ExecutionState` claims it is — one run's resolved inputs
    travelling as a unit — rather than a temporary built only to be unpacked.

    Parameters
    ----------
    cmd : SafeCmd
        The validated command to run.
    state : _ExecutionState
        The run's already-resolved inputs.

    Returns
    -------
    CommandResult
        The completed command's result.
    """
    output = state.output
    # The bracket owns the session for the whole run: a plan observer, a
    # before hook, or anything else that raises before execution starts
    # still finalizes the adapter's framing rather than stranding an open
    # group, and the guard below closes exactly what those paths leave.
    sink_bracket = _SinkBracket.open(
        output.sink,
        _command_session_start(cmd),
    )
    tracking = _ExecutionTracking(
        execution_hooks=_collect_hooks(current_context()),
        pending_tasks=[],
        sink_bracket=sink_bracket,
    )
    observation: _StageObservation | None = None
    try:
        observation = _prepare_execution_observation(
            cmd,
            state.context,
            tracking,
            output,
        )
        observation.emit("plan", _EventDetails(pid=None))
        for hook in tracking.execution_hooks.before_hooks:
            hook(cmd)
        return await _execute_with_hooks(
            cmd,
            _build_subprocess_execution(
                cmd,
                state,
                observation=observation,
                sink_session=tracking.sink_bracket.session,
            ),
            tracking,
        )
    except BaseException as run_error:
        await _finalize_prepared_command_failure(tracking, observation, run_error)
        raise
