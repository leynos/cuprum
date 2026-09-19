"""Internal single-command execution coordination for ``cuprum.sh``.

This module is the private machinery behind ``SafeCmd.run``/``run_sync``. It
is the single-command counterpart of :mod:`cuprum._pipeline_internals`: it
prepares one validated command's observation, bundles everything that run
needs before it spawns, drives that bundle through the subprocess layer, and
finalizes the run's presentation-sink session on every terminal path. The
public command surface, the value types it exchanges, and pipeline
orchestration stay in :mod:`cuprum.sh`; this module holds only the sequencing
that a single command's execution owes.

Finalization is the reason the sequence lives in one place. When execution
fails or an after-hook raises, the sink session must be closed *before* the
observe-hook tasks are drained: the drain aggregates a hook failure with the
error that ended the run, so closing afterwards would record the aggregate —
an ``error`` annotation standing in for a timeout — and a drain that raised
would skip the close entirely. The drain itself runs through
:func:`cuprum._process_lifecycle._shielded_cleanup` rather than a bare
``await asyncio.shield(...)``, which keeps cancellation off the drain but
still resumes the awaiting coroutine immediately and would leak exactly the
tasks the drain exists to reconcile.

It collaborates with :mod:`cuprum._sink_lifecycle` (the session bracket it
owns), :mod:`cuprum._subprocess_execution`, :mod:`cuprum._subprocess_context`,
:mod:`cuprum._observability`, :mod:`cuprum._idle_heartbeat`,
:mod:`cuprum._pipeline_types`, :mod:`cuprum._pipeline_internals` (hook
collection), and :mod:`cuprum.context`, and is invoked by :mod:`cuprum.sh`.
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
from cuprum.context import current_context

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


def _prepare_execution_observation(
    cmd: SafeCmd,
    context: ExecutionContext,
    tracking: _ExecutionTracking,
    output: RunOutputOptions,
) -> _StageObservation:
    """Prepare the observation context for command execution."""
    cwd = Path(context.cwd) if context.cwd is not None else None
    env_overlay = _resolve_env_overlay(context.env)
    tags = _merge_tags(
        _base_stage_tags(
            cmd,
            capture=output.capture,
            echo_stdout=output.resolved_echo[0],
            echo_stderr=output.resolved_echo[1],
        ),
        context.tags,
    )
    return _StageObservation(
        cmd=cmd,
        hooks=tracking.execution_hooks,
        cwd=cwd,
        env_overlay=env_overlay,
        tags=tags,
        pending_tasks=tracking.pending_tasks,
        wall_clock=time.time,
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
        sink_session=sink_session,
        timeout=state.timeout,
        observation=observation,
        stdin_data=state.stdin_data,
        on_line=state.output.on_line,
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
    """Execute *execution*, dispatch after-hooks, and handle cancellation.

    Draining the observe-hook tasks during cleanup must not let a failing
    background hook stand in for the error that triggered the cleanup: a caller
    awaiting ``TimeoutExpired`` (or a cancellation) would otherwise see the
    hook's exception instead. Both cleanup paths therefore drain through
    :func:`_drain_tasks_during_cleanup`, which aggregates a drain failure with
    the active error into a ``BaseExceptionGroup`` rather than replacing it —
    matching the pipeline path. The drain on the success path still surfaces a
    hook failure directly, because there is no primary error to preserve.

    Every drain runs through :func:`_shielded_cleanup` rather than a bare
    ``await asyncio.shield(...)``. The shield alone keeps the cancellation off
    the drain, but the *awaiting* coroutine resumes immediately, so the run
    would propagate its ``CancelledError`` while the hook tasks were still
    settling — leaking exactly the tasks the drain exists to reconcile.

    Returns
    -------
    CommandResult
        The completed command's result, once every after-hook has run and the
        observe-hook tasks have drained.
    """
    try:
        result = await _execute_subprocess(execution)
        for hook in tracking.execution_hooks.after_hooks:
            hook(cmd, result)
    except BaseException as run_error:
        # Close before the drain. The drain aggregates a hook failure with the
        # error that ended the run, so closing afterwards would record the
        # aggregate — an ``error`` annotation standing in for a timeout — and
        # a drain that raised would skip the close entirely.
        tracking.sink_bracket.close(outcome=_outcome_for_error(run_error))
        await _shielded_cleanup(
            _drain_tasks_during_cleanup(
                tracking.pending_tasks,
                run_error,
                message=_COMMAND_FINALIZATION_ERROR,
            )
        )
        raise
    tracking.sink_bracket.close(outcome=_outcome_for_result(result))
    await _shielded_cleanup(_wait_for_exec_hook_tasks(tracking.pending_tasks))
    return result


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
        # Close before the drain, for the reason _execute_with_hooks gives: the
        # drain aggregates a hook failure with the error that ended the run, so
        # closing afterwards would record the aggregate — an ``error``
        # annotation standing in for a timeout.
        tracking.sink_bracket.close(outcome=_outcome_for_error(run_error))
        # The plan event above can schedule observe tasks before a later
        # observer or before-hook raises, and no downstream helper owns them
        # yet, so the run owes the drain here. The path that already drained in
        # _execute_with_hooks finds an empty list and returns immediately, so
        # this cannot double-drain.
        await _shielded_cleanup(
            _drain_tasks_during_cleanup(
                tracking.pending_tasks,
                run_error,
                message=_COMMAND_FINALIZATION_ERROR,
            )
        )
        raise
