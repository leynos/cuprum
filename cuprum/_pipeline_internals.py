"""Internal pipeline execution coordination and fail-fast semantics.

This module is the private machinery behind ``cuprum.sh``'s
``Pipeline.run``/``run_sync``. It ties together allowlist enforcement
and hook collection, stage process spawning, inter-stage pipe wiring,
completion waiting with optional timeouts, and per-stage
``CommandResult`` assembly. It exists chiefly to centralise
finalization: when a stage fails or an after-hook raises, pending
observe-hook tasks must still be drained and every independent
failure preserved, grouping after-hook and task failures into a
``BaseExceptionGroup``. It collaborates with ``cuprum._process_lifecycle``,
``cuprum._pipeline_collect``, ``cuprum._pipeline_streams``,
``cuprum._pipeline_types``, ``cuprum._pipeline_wait``,
``cuprum._observability``, and
``cuprum.context``, and is invoked by ``cuprum.sh`` and
``cuprum._subprocess_execution``/``_process_lifecycle``.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

from cuprum._observability import (
    _base_stage_tags,
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
from cuprum._pipeline_streams import (
    _cancel_stream_tasks,
    _PipelineRunConfig,
)
from cuprum._pipeline_types import (
    _EventDetails,
    _ExecutionHooks,
    _PipelineSpawnResult,
    _PipelineStageResultInputs,
    _StageObservation,
)
from cuprum._process_lifecycle import _spawn_pipeline_processes
from cuprum.context import current_context

if typ.TYPE_CHECKING:
    import asyncio

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
                _base_stage_tags(cmd, capture=config.capture, echo=config.echo),
                {
                    "pipeline_stage_index": idx,
                    "pipeline_stages": len(parts),
                },
                config.ctx.tags,
            ),
            cwd=cwd,
            env_overlay=env_overlay,
            pending_tasks=pending_tasks,
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


def _build_pipeline_stage_results(
    parts: tuple[SafeCmd, ...],
    observations: tuple[_StageObservation, ...],
    *,
    processes: list[asyncio.subprocess.Process],
    inputs: _PipelineStageResultInputs,
) -> list[CommandResult]:
    """Emit exit events and assemble a command result per pipeline stage."""
    sh = _sh_module()
    stage_results: list[CommandResult] = []
    for idx, obs in enumerate(observations):
        process = processes[idx]
        ended_at = inputs.wait_result.ended_at[idx]
        duration_s = (
            None
            if ended_at is None
            else max(0.0, ended_at - inputs.wait_result.started_at[idx])
        )
        obs.emit(
            "exit",
            _EventDetails(
                pid=process.pid,
                exit_code=inputs.wait_result.exit_codes[idx],
                duration_s=duration_s,
            ),
        )
        stage_results.append(
            sh.CommandResult(
                program=obs.cmd.program,
                argv=obs.cmd.argv,
                exit_code=inputs.wait_result.exit_codes[idx],
                pid=process.pid if process.pid is not None else -1,
                stdout=inputs.final_stdout if idx == len(parts) - 1 else None,
                stderr=inputs.stderr_by_stage[idx],
            ),
        )
    return stage_results


async def _drain_tasks_during_cleanup(
    pending_tasks: list[asyncio.Task[None]],
    active_error: BaseException,
    *,
    message: str = _PIPELINE_FINALIZATION_ERROR,
) -> None:
    """Drain observe tasks in cleanup, aggregating a failure with ``active_error``.

    Shared by the pipeline and single-command cleanup paths; ``message`` names
    whichever finalization failed.
    """
    try:
        await _wait_for_exec_hook_tasks(pending_tasks)
    # Catch every task failure, including non-Exception BaseExceptions, so
    # cleanup cannot mask the error that triggered it.
    except BaseException as task_error:  # noqa: BLE001
        raise BaseExceptionGroup(
            message,
            (active_error, task_error),
        ) from None


async def _finalize_pipeline_execution(
    parts: tuple[SafeCmd, ...],
    observations: tuple[_StageObservation, ...],
    stage_results: list[CommandResult],
    pending_tasks: list[asyncio.Task[None]],
) -> None:
    """Run after hooks for every stage and drain pending observe tasks."""
    hooks_by_stage = tuple(obs.hooks for obs in observations)
    try:
        _run_pipeline_after_hooks(parts, hooks_by_stage, stage_results)
    except BaseException as after_hook_error:
        await _drain_tasks_during_cleanup(pending_tasks, after_hook_error)
        raise
    await _wait_for_exec_hook_tasks(pending_tasks)


async def _run_pipeline(
    parts: tuple[SafeCmd, ...],
    config: _PipelineRunConfig,
) -> PipelineResult:
    """Execute a pipeline and return a structured result."""
    pending_tasks: list[asyncio.Task[None]] = []
    observations = _build_pipeline_observations(
        parts,
        config,
        pending_tasks=pending_tasks,
    )
    try:
        _emit_plan_events_and_run_before_hooks(observations)
        (
            processes,
            stderr_tasks,
            stdout_task,
            started_at,
        ) = await _spawn_pipeline_processes(
            parts,
            config,
            observations=observations,
        )
        spawn = _PipelineSpawnResult(
            processes=processes,
            stderr_tasks=stderr_tasks,
            stdout_task=stdout_task,
            started_at=started_at,
        )
    except BaseException as spawn_error:
        await _drain_tasks_during_cleanup(pending_tasks, spawn_error)
        raise
    try:
        inputs = await _collect_pipeline_inputs(
            parts,
            spawn,
            config,
        )
    except _sh_module().TimeoutExpired as timeout_error:
        await _drain_tasks_during_cleanup(pending_tasks, timeout_error)
        raise
    except BaseException as run_error:
        await _cancel_stream_tasks(spawn.stderr_tasks, spawn.stdout_task)
        await _drain_tasks_during_cleanup(pending_tasks, run_error)
        raise
    stage_results = _build_pipeline_stage_results(
        parts,
        observations,
        processes=spawn.processes,
        inputs=inputs,
    )
    await _finalize_pipeline_execution(
        parts,
        observations,
        stage_results,
        pending_tasks,
    )

    return _sh_module().PipelineResult(
        stages=tuple(stage_results),
        failure_index=inputs.wait_result.failure_index,
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
