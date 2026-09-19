"""Internal pipeline execution coordination and fail-fast semantics.

This module is the private machinery behind ``cuprum.sh``'s
``Pipeline.run``/``run_sync``. It ties together allowlist enforcement
and hook collection, stage process spawning, inter-stage pipe wiring,
completion waiting with optional timeouts, and per-stage
``CommandResult`` assembly. It exists chiefly to centralize
finalization: when a stage fails or an after-hook raises, pending
observe-hook tasks must still be drained and every independent
failure preserved, grouping after-hook and task failures into a
``BaseExceptionGroup``. It collaborates with ``cuprum._pipeline_spawn``,
``cuprum._pipeline_collect``, ``cuprum._pipeline_sink`` (the pipeline's
result mapping), ``cuprum._pipeline_streams``, ``cuprum._pipeline_types``,
``cuprum._pipeline_wait``, ``cuprum._process_lifecycle``,
``cuprum._sink_lifecycle`` (the sink session it brackets the run with),
``cuprum._observability``, and
``cuprum.context``, and is invoked by ``cuprum.sh`` and
``cuprum._subprocess_execution``.
"""

from __future__ import annotations

import time
import typing as typ
from pathlib import Path

from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._observability import (
    _base_stage_tags,
    _drain_tasks_during_cleanup,
    _merge_tags,
    _resolve_env_overlay,
    _wait_for_exec_hook_tasks,
)
from cuprum._pipeline_collect import (
    _await_pipeline_wait_result,
    _build_timeout_expired_error,
    _collect_pipeline_inputs,
    _gather_pipeline_outputs,
    _sh_module,
)
from cuprum._pipeline_results import (
    _build_pipeline_stage_results,
    _emit_timeout_exit_events,
)
from cuprum._pipeline_sink import _pipeline_result_outcome
from cuprum._pipeline_spawn import _spawn_pipeline_processes
from cuprum._pipeline_stream_results import _cancel_stream_tasks
from cuprum._pipeline_types import (
    _EventDetails,
    _ExecutionHooks,
    _PipelineObservers,
    _PipelineSpawnResult,
    _StageObservation,
    _StageWaitContext,
)
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._sink_lifecycle import _outcome_for_error, _SinkBracket
from cuprum._timeout_reporting import _report_pipeline_timeout_expiry
from cuprum.context import current_context

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._pipeline_config import _PipelineRunConfig
    from cuprum.context import CuprumContext
    from cuprum.sh import CommandResult, PipelineResult, SafeCmd

__all__ = [
    "_await_pipeline_wait_result",
    "_build_timeout_expired_error",
    "_collect_pipeline_inputs",
    "_gather_pipeline_outputs",
    "_sh_module",
]

_MIN_PIPELINE_STAGES = 2
_PIPELINE_FINALIZATION_ERROR = "pipeline finalization failed"


def _enforce_allowlist(cmd: SafeCmd) -> None:
    """Reject ``cmd`` when the active context forbids its program."""
    current_context().check_allowed(cmd.program)


def _collect_hooks(ctx: CuprumContext) -> _ExecutionHooks:
    """Return the before/after/observe hooks registered on ``ctx``."""
    return _ExecutionHooks(
        before_hooks=ctx.before_hooks,
        after_hooks=ctx.after_hooks,
        observe_hooks=ctx.observe_hooks,
    )


def _build_pipeline_observations(
    parts: tuple[SafeCmd, ...],
    config: _PipelineRunConfig,
    *,
    pending_tasks: list[asyncio.Task[None]],
) -> tuple[_StageObservation, ...]:
    """Build per-stage observation state for every command in the pipeline."""
    for cmd in parts:
        _enforce_allowlist(cmd)
    ctx = current_context()
    hooks_by_stage = tuple(_collect_hooks(ctx) for _ in parts)
    cwd = None if config.ctx.cwd is None else Path(config.ctx.cwd)
    env_overlay = _resolve_env_overlay(config.ctx.env)
    return tuple(
        _StageObservation(
            cmd=cmd,
            hooks=hooks,
            tags=_merge_tags(
                _base_stage_tags(
                    cmd,
                    capture=config.capture,
                    echo_stdout=config.echo_stdout,
                    echo_stderr=config.echo_stderr,
                ),
                {
                    "pipeline_stage_index": idx,
                    "pipeline_stages": len(parts),
                },
                config.ctx.tags,
            ),
            cwd=cwd,
            env_overlay=env_overlay,
            pending_tasks=pending_tasks,
            wall_clock=time.time,
        )
        for idx, (cmd, hooks) in enumerate(zip(parts, hooks_by_stage, strict=True))
    )


def _emit_plan_events_and_run_before_hooks(
    observations: tuple[_StageObservation, ...],
) -> None:
    """Emit plan events and run before hooks for every stage."""
    for obs in observations:
        obs.emit("plan", _EventDetails(pid=None))
        for hook in obs.hooks.before_hooks:
            hook(obs.cmd)


async def _finalize_pipeline_execution(
    parts: tuple[SafeCmd, ...],
    observers: _PipelineObservers,
    stage_results: list[CommandResult],
    sink_bracket: _SinkBracket,
) -> None:
    """Run after hooks, commit the sink outcome, then drain observe tasks.

    This is the pipeline's counterpart to the command path's
    :func:`cuprum._command_internals._execute_with_hooks`, and the three steps
    are in that order for the same reason: an after-hook that raises is a
    terminal *run* error, so the outcome the adapter records has to be decided
    after the hooks have had their say. Closing with the stage-result outcome
    first would leave the adapter reporting ``exit_zero`` for a run that ended
    in an exception, and :meth:`_SinkBracket.close` clears its session on the
    first call, so the error close that followed would be a no-op.

    Both drains are shielded. The pipeline owns these observe-hook tasks, so a
    cancellation landing while finalization waits on them must not return
    before they have settled — that would leak a task per pending hook.
    """
    observations = observers.observations
    pending_tasks = observers.pending_tasks
    hooks_by_stage = tuple(obs.hooks for obs in observations)
    try:
        _run_pipeline_after_hooks(parts, hooks_by_stage, stage_results)
    except BaseException as after_hook_error:
        sink_bracket.close(outcome=_outcome_for_error(after_hook_error))
        await _shielded_cleanup(
            _drain_tasks_during_cleanup(
                pending_tasks, after_hook_error, message=_PIPELINE_FINALIZATION_ERROR
            )
        )
        raise
    sink_bracket.close(outcome=_pipeline_result_outcome(stage_results))
    await _shielded_cleanup(_wait_for_exec_hook_tasks(pending_tasks))


async def _reconcile_pipeline_run_failure(
    spawn: _PipelineSpawnResult,
    pending_tasks: list[asyncio.Task[None]],
    run_error: BaseException,
) -> None:
    """Cancel the stream tasks and drain the observe tasks after a run failure.

    Kept as one coroutine so the caller can shield both halves together: the
    stream tasks and the observe-hook tasks are all owned by the pipeline, and
    a cancellation arriving between two separately shielded steps would leave
    the second set pending.
    """
    await _cancel_stream_tasks(spawn.stderr_tasks, spawn.stdout_task)
    await _drain_tasks_during_cleanup(
        pending_tasks, run_error, message=_PIPELINE_FINALIZATION_ERROR
    )


async def _finalize_pipeline_timeout(
    config: _PipelineRunConfig,
    spawn: _PipelineSpawnResult,
    observers: _PipelineObservers,
    timeout_error: BaseException,
) -> None:
    """Report a pipeline timeout and finalize its sink and observe tasks."""
    observations = observers.observations
    config.sink_bracket.close(outcome=_outcome_for_error(timeout_error))
    _report_pipeline_timeout_expiry(
        observations,
        spawn.processes,
        configured_timeout=config.timeout,
    )
    _emit_timeout_exit_events(observations, spawn)
    await _shielded_cleanup(
        _drain_tasks_during_cleanup(
            observers.pending_tasks,
            timeout_error,
            message=_PIPELINE_FINALIZATION_ERROR,
        )
    )


async def _run_spawned_pipeline(
    parts: tuple[SafeCmd, ...],
    config: _PipelineRunConfig,
    spawn: _PipelineSpawnResult,
    observers: _PipelineObservers,
) -> PipelineResult:
    """Drive a spawned pipeline to a result, reconciling whatever ends it.

    Split from :func:`_run_pipeline`, which keeps the pre-spawn half. The
    stages are running by the time this is entered, so every exit path owes
    them teardown; keeping those paths together is what makes the set
    reviewable.

    The two failure branches stay distinct because they owe different debts. A
    deadline has already terminated the stages, so it reports the expiry and
    emits each stage's terminal ``exit`` before draining the observe tasks.
    Anything else — a cancellation, most often — still has live stream tasks,
    which is why it goes through :func:`_reconcile_pipeline_run_failure`.

    Returns
    -------
    PipelineResult
        The assembled stage results and the index of the first failing stage.
    """
    observations = observers.observations
    pending_tasks = observers.pending_tasks
    sink_bracket = config.sink_bracket
    try:
        inputs = await _collect_pipeline_inputs(
            parts,
            spawn,
            config,
        )
    except _sh_module().TimeoutExpired as timeout_error:
        await _finalize_pipeline_timeout(
            config,
            spawn,
            observers,
            timeout_error,
        )
        raise
    except BaseException as run_error:
        sink_bracket.close(outcome=_outcome_for_error(run_error))
        # One shielded unit: shielding the two separately would let a
        # cancellation landing between them abandon the observe-hook drain.
        await _shielded_cleanup(
            _reconcile_pipeline_run_failure(spawn, pending_tasks, run_error)
        )
        raise
    try:
        stage_results = _build_pipeline_stage_results(
            parts,
            observations,
            processes=spawn.processes,
            inputs=inputs,
        )
    except BaseException as result_error:
        sink_bracket.close(outcome=_outcome_for_error(result_error))
        # The stage-result build sits between the spawn and finalization, so
        # the observe-hook tasks this pipeline owns are nobody else's yet: the
        # run owes the drain here for the same reason the spawn-failure branch
        # above does, and for the same reason the close comes first.
        await _shielded_cleanup(
            _drain_tasks_during_cleanup(
                pending_tasks,
                result_error,
                message=_PIPELINE_FINALIZATION_ERROR,
            )
        )
        raise
    # Finalization owns the close, after the after-hooks have run: a failing
    # after-hook is a terminal run error, so the outcome cannot be committed
    # before the hooks have had their say.
    await _finalize_pipeline_execution(
        parts,
        observers,
        stage_results,
        sink_bracket,
    )

    return _sh_module().PipelineResult(
        stages=tuple(stage_results),
        failure_index=inputs.wait_result.failure_index,
    )


async def _run_pipeline(
    parts: tuple[SafeCmd, ...],
    config: _PipelineRunConfig,
) -> PipelineResult:
    """Execute a pipeline and return a structured result.

    A thin wrapper, so that the aggregate idle heartbeat is settled on every
    exit path without threading a ``finally`` through the spawn and drive
    halves below.

    Returns
    -------
    PipelineResult
        The assembled stage results and the index of the first failing stage.
    """
    try:
        return await _spawn_and_drive_pipeline(parts, config)
    finally:
        # Completion, a deadline, cancellation, or a partial spawn: whichever
        # ended this run, it has stopped producing output. Stopping is
        # idempotent, so the earlier stops are not undone by this one.
        await _shielded_cleanup(_stop_idle_monitor(config.idle))


async def _spawn_and_drive_pipeline(
    parts: tuple[SafeCmd, ...],
    config: _PipelineRunConfig,
) -> PipelineResult:
    """Spawn every stage, then drive the spawned pipeline to a result."""
    pending_tasks: list[asyncio.Task[None]] = []
    try:
        # Inside the guard: building the observations enforces the allowlist,
        # so a denied stage must still finalize the run's framing.
        observations = _build_pipeline_observations(
            parts,
            config,
            pending_tasks=pending_tasks,
        )
        _emit_plan_events_and_run_before_hooks(observations)
        (
            processes,
            stderr_tasks,
            stdout_task,
            started_at,
            relay_diagnostics_by_stage,
        ) = await _spawn_pipeline_processes(
            parts,
            config,
            observations=observations,
        )
        spawn = _PipelineSpawnResult(
            processes=processes,
            stderr_tasks=stderr_tasks,
            stdout_task=stdout_task,
            relay_diagnostics_by_stage=tuple(relay_diagnostics_by_stage),
            stages=_StageWaitContext(
                started_at=tuple(started_at),
                observations=observations,
            ),
            idle=config.idle,
        )
    except BaseException as spawn_error:
        config.sink_bracket.close(outcome=_outcome_for_error(spawn_error))
        await _shielded_cleanup(
            _drain_tasks_during_cleanup(
                pending_tasks, spawn_error, message=_PIPELINE_FINALIZATION_ERROR
            )
        )
        raise
    return await _run_spawned_pipeline(
        parts,
        config,
        spawn,
        _PipelineObservers(observations, pending_tasks),
    )


def _run_pipeline_after_hooks(
    parts: tuple[SafeCmd, ...],
    hooks_by_stage: tuple[_ExecutionHooks, ...],
    results: list[CommandResult],
) -> None:
    """Run registered after hooks for each pipeline stage."""
    for cmd, hooks, result in zip(parts, hooks_by_stage, results, strict=True):
        for hook in hooks.after_hooks:
            hook(cmd, result)
