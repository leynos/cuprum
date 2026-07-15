"""Internal pipeline execution coordination and fail-fast semantics."""

from __future__ import annotations

import asyncio
import sys
import time
import typing as typ
from pathlib import Path

from cuprum._observability import (
    _base_stage_tags,
    _merge_tags,
    _resolve_env_overlay,
    _wait_for_exec_hook_tasks,
)
from cuprum._pipeline_spawn import _spawn_pipeline_processes
from cuprum._pipeline_streams import (
    _cancel_stream_tasks,
    _create_pipe_tasks,
    _gather_optional_text_tasks,
    _PipelineRunConfig,
)
from cuprum._pipeline_types import (
    _EventDetails,
    _ExecutionHooks,
    _PipelineOutputs,
    _PipelineSpawnResult,
    _PipelineStageResultInputs,
    _StageObservation,
)
from cuprum._pipeline_wait import _PipelineWaitResult, _wait_for_pipeline
from cuprum.context import current_context

if typ.TYPE_CHECKING:
    import types

    from cuprum.context import CuprumContext
    from cuprum.sh import CommandResult, PipelineResult, SafeCmd

_MIN_PIPELINE_STAGES = 2


def _sh_module() -> types.ModuleType:
    """Return the imported ``cuprum.sh`` module or raise if it is absent."""
    module = sys.modules.get("cuprum.sh")
    if module is None:
        msg = "cuprum.sh must be imported before running pipelines"
        raise RuntimeError(msg)
    return module


def _enforce_allowlist(cmd: SafeCmd) -> None:
    """Enforce the active allowlist for ``cmd``.

    This is a command: its purpose is the side effect of rejecting a program
    that the current context does not permit. It raises
    :class:`~cuprum.context.ForbiddenProgramError` for a forbidden program and
    returns ``None`` otherwise.
    """
    current_context().check_allowed(cmd.program)


def _collect_hooks(ctx: CuprumContext) -> _ExecutionHooks:
    """Return the before/after/observe hooks registered on ``ctx``.

    This is a pure query with no side effects; allowlist enforcement is the
    separate responsibility of :func:`_enforce_allowlist`.
    """
    return _ExecutionHooks(
        before_hooks=ctx.before_hooks,
        after_hooks=ctx.after_hooks,
        observe_hooks=ctx.observe_hooks,
    )


async def _await_pipeline_wait_result(
    spawn: _PipelineSpawnResult,
    config: _PipelineRunConfig,
    *,
    timeout_deadline: float | None,
) -> _PipelineWaitResult:
    """Wait for the pipeline to finish, honouring any timeout deadline."""
    wait_timeout: float | None = None
    if timeout_deadline is not None:
        wait_timeout = max(0.0, timeout_deadline - time.monotonic())
    if wait_timeout is None:
        return await _wait_for_pipeline(
            spawn.processes,
            pipe_tasks=_create_pipe_tasks(spawn.processes),
            cancel_grace=config.ctx.cancel_grace,
            started_at=spawn.started_at,
        )
    return await asyncio.wait_for(
        _wait_for_pipeline(
            spawn.processes,
            pipe_tasks=_create_pipe_tasks(spawn.processes),
            cancel_grace=config.ctx.cancel_grace,
            started_at=spawn.started_at,
        ),
        wait_timeout,
    )


async def _gather_pipeline_outputs(
    spawn: _PipelineSpawnResult,
) -> tuple[tuple[str | None, ...], str | None]:
    """Gather stderr by stage and final stdout from spawn tasks."""
    stderr_by_stage = await _gather_optional_text_tasks(spawn.stderr_tasks)
    final_stdout = None if spawn.stdout_task is None else await spawn.stdout_task
    return stderr_by_stage, final_stdout


def _build_timeout_expired_error(
    parts: tuple[SafeCmd, ...],
    timeout: float,
    outputs: _PipelineOutputs,
) -> BaseException:
    """Construct a TimeoutExpired exception with captured outputs."""
    stderr_text = None
    if outputs.capture:
        stderr_text = "".join(text or "" for text in outputs.stderr_by_stage)
    output = outputs.final_stdout if outputs.capture else None
    return _sh_module().TimeoutExpired(
        cmd=tuple(cmd.argv_with_program for cmd in parts),
        timeout=timeout,
        output=output,
        stderr=stderr_text,
    )


async def _collect_pipeline_inputs(
    parts: tuple[SafeCmd, ...],
    spawn: _PipelineSpawnResult,
    config: _PipelineRunConfig,
) -> _PipelineStageResultInputs:
    """Await pipeline completion and collect outputs, mapping timeouts."""
    timeout = config.timeout
    timeout_deadline: float | None = None
    if timeout is not None:
        timeout_deadline = time.monotonic() + timeout

    try:
        wait_result = await _await_pipeline_wait_result(
            spawn,
            config,
            timeout_deadline=timeout_deadline,
        )
        stderr_by_stage, final_stdout = await _gather_pipeline_outputs(spawn)
        return _PipelineStageResultInputs(
            wait_result=wait_result,
            stderr_by_stage=stderr_by_stage,
            final_stdout=final_stdout,
        )
    except TimeoutError as exc:
        stderr_by_stage, final_stdout = await _gather_pipeline_outputs(spawn)
        if timeout is None:
            msg = "TimeoutError without a configured timeout"
            raise RuntimeError(msg) from exc
        outputs = _PipelineOutputs(
            stderr_by_stage=stderr_by_stage,
            final_stdout=final_stdout,
            capture=config.capture,
        )
        raise _build_timeout_expired_error(parts, timeout, outputs) from exc


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
    finally:
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
    except BaseException:
        await _wait_for_exec_hook_tasks(pending_tasks)
        raise
    try:
        inputs = await _collect_pipeline_inputs(
            parts,
            spawn,
            config,
        )
    except _sh_module().TimeoutExpired:
        await _wait_for_exec_hook_tasks(pending_tasks)
        raise
    except BaseException:
        await _cancel_stream_tasks(spawn.stderr_tasks, spawn.stdout_task)
        await _wait_for_exec_hook_tasks(pending_tasks)
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
