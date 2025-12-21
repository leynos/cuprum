"""Internal pipeline execution coordination and fail-fast semantics."""

from __future__ import annotations

import dataclasses as dc
import sys
import time
import typing as typ
from pathlib import Path

from cuprum._observability import (
    _emit_exec_event,
    _freeze_str_mapping,
    _merge_tags,
    _wait_for_exec_hook_tasks,
)
from cuprum._pipeline_spawn import _spawn_pipeline_processes
from cuprum._pipeline_streams import (
    _cancel_stream_tasks,
    _create_pipe_tasks,
    _gather_optional_text_tasks,
    _PipelineRunConfig,
    _prepare_pipeline_config,
)
from cuprum._pipeline_wait import _PipelineWaitResult, _wait_for_pipeline
from cuprum.context import current_context
from cuprum.events import ExecEvent

if typ.TYPE_CHECKING:
    import asyncio
    import types

    from cuprum.context import AfterHook, BeforeHook
    from cuprum.events import ExecHook
    from cuprum.sh import CommandResult, ExecutionContext, PipelineResult, SafeCmd

_MIN_PIPELINE_STAGES = 2


def _sh_module() -> types.ModuleType:
    module = sys.modules.get("cuprum.sh")
    if module is None:
        msg = "cuprum.sh must be imported before running pipelines"
        raise RuntimeError(msg)
    return module


@dc.dataclass(frozen=True, slots=True)
class _ExecutionHooks:
    before_hooks: tuple[BeforeHook, ...]
    after_hooks: tuple[AfterHook, ...]
    observe_hooks: tuple[ExecHook, ...]


def _run_before_hooks(cmd: SafeCmd) -> _ExecutionHooks:
    """Collect hooks for a command after enforcing the current allowlist."""
    ctx = current_context()
    ctx.check_allowed(cmd.program)
    return _ExecutionHooks(
        before_hooks=ctx.before_hooks,
        after_hooks=ctx.after_hooks,
        observe_hooks=ctx.observe_hooks,
    )


@dc.dataclass(frozen=True, slots=True)
class _StageObservation:
    cmd: SafeCmd
    hooks: _ExecutionHooks
    tags: typ.Mapping[str, object]
    cwd: Path | None
    env_overlay: typ.Mapping[str, str] | None
    pending_tasks: list[asyncio.Task[None]]

    def emit(
        self,
        phase: typ.Literal["plan", "start", "stdout", "stderr", "exit"],
        details: _EventDetails,
    ) -> None:
        if not self.hooks.observe_hooks:
            return
        _emit_exec_event(
            self.hooks.observe_hooks,
            ExecEvent(
                phase=phase,
                program=self.cmd.program,
                argv=self.cmd.argv_with_program,
                cwd=self.cwd,
                env=self.env_overlay,
                pid=details.pid,
                timestamp=time.time(),
                line=details.line,
                exit_code=details.exit_code,
                duration_s=details.duration_s,
                tags=self.tags,
            ),
            pending_tasks=self.pending_tasks,
        )


@dc.dataclass(frozen=True, slots=True)
class _EventDetails:
    pid: int | None
    line: str | None = None
    exit_code: int | None = None
    duration_s: float | None = None


@dc.dataclass(frozen=True, slots=True)
class _PipelineStageResultInputs:
    wait_result: _PipelineWaitResult
    stderr_by_stage: tuple[str | None, ...]
    final_stdout: str | None


def _build_pipeline_observations(
    parts: tuple[SafeCmd, ...],
    config: _PipelineRunConfig,
    *,
    pending_tasks: list[asyncio.Task[None]],
) -> tuple[_StageObservation, ...]:
    hooks_by_stage = tuple(_run_before_hooks(cmd) for cmd in parts)
    cwd = None if config.ctx.cwd is None else Path(config.ctx.cwd)
    env_overlay = _freeze_str_mapping(config.ctx.env)
    return tuple(
        _StageObservation(
            cmd=cmd,
            hooks=hooks,
            tags=_merge_tags(
                {
                    "project": cmd.project.name,
                    "capture": config.capture,
                    "echo": config.echo,
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
    hooks_by_stage = tuple(obs.hooks for obs in observations)
    _run_pipeline_after_hooks(parts, hooks_by_stage, stage_results)
    await _wait_for_exec_hook_tasks(pending_tasks)


async def _run_pipeline(
    parts: tuple[SafeCmd, ...],
    *,
    capture: bool,
    echo: bool,
    context: ExecutionContext | None,
) -> PipelineResult:
    """Execute a pipeline and return a structured result."""
    config = _prepare_pipeline_config(capture=capture, echo=echo, context=context)
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
    except BaseException:
        await _wait_for_exec_hook_tasks(pending_tasks)
        raise
    try:
        wait_result = await _wait_for_pipeline(
            processes,
            pipe_tasks=_create_pipe_tasks(processes),
            cancel_grace=config.ctx.cancel_grace,
            started_at=started_at,
        )
        inputs = _PipelineStageResultInputs(
            wait_result=wait_result,
            stderr_by_stage=await _gather_optional_text_tasks(stderr_tasks),
            final_stdout=None if stdout_task is None else await stdout_task,
        )
    except BaseException:
        await _cancel_stream_tasks(stderr_tasks, stdout_task)
        await _wait_for_exec_hook_tasks(pending_tasks)
        raise
    stage_results = _build_pipeline_stage_results(
        parts,
        observations,
        processes=processes,
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
