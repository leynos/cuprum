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
``cuprum._pipeline_streams``, ``cuprum._pipeline_types``,
``cuprum._pipeline_wait``, ``cuprum._observability``, and
``cuprum.context``, and is invoked by ``cuprum.sh`` and
``cuprum._subprocess_execution``/``_process_lifecycle``.
"""

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
from cuprum._pipeline_results import (
    _build_pipeline_stage_results,
    _emit_timeout_exit_events,
)
from cuprum._pipeline_streams import (
    _cancel_stream_tasks,
    _create_pipe_tasks,
    _gather_optional_text_tasks,
    _PipelineRunConfig,
    _reconcile_pipe_tasks,
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
from cuprum._process_lifecycle import (
    _spawn_pipeline_processes,
    _terminate_timed_out_stages,
)
from cuprum._timeout_reporting import _report_pipeline_timeout_expiry
from cuprum.context import current_context

if typ.TYPE_CHECKING:
    import types

    from cuprum.context import CuprumContext
    from cuprum.sh import CommandResult, PipelineResult, SafeCmd

_MIN_PIPELINE_STAGES = 2
_PIPELINE_FINALIZATION_ERROR = "pipeline finalization failed"


def _sh_module() -> types.ModuleType:
    """Return the imported ``cuprum.sh`` module or raise if it is absent."""
    module = sys.modules.get("cuprum.sh")
    if module is None:
        msg = "cuprum.sh must be imported before running pipelines"
        raise RuntimeError(msg)
    return module


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


async def _await_pipeline_wait_result(
    spawn: _PipelineSpawnResult,
    config: _PipelineRunConfig,
    *,
    timeout_deadline: float | None,
    pipe_tasks: list[asyncio.Task[None]],
) -> _PipelineWaitResult:
    """Wait for the pipeline to finish, honouring any timeout deadline.

    ``pipe_tasks`` belongs to the caller: a non-positive deadline cancels
    ``_wait_for_pipeline`` before the ``finally`` that would reconcile them,
    so the caller reconciles them instead (see the developers' guide).
    """
    wait_timeout: float | None = None
    if timeout_deadline is not None:
        wait_timeout = max(0.0, timeout_deadline - time.monotonic())
    if wait_timeout is None:
        return await _wait_for_pipeline(
            spawn.processes,
            pipe_tasks=pipe_tasks,
            cancel_grace=config.ctx.cancel_grace,
            started_at=spawn.started_at,
        )
    return await asyncio.wait_for(
        _wait_for_pipeline(
            spawn.processes,
            pipe_tasks=pipe_tasks,
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
    """Await pipeline completion and collect outputs, mapping timeouts.

    The inter-stage pumps are owned here, so they are reconciled in a
    ``finally`` that covers every exit: success, the deadline path, caller
    cancellation, and the non-positive deadline that cancels
    :func:`_wait_for_pipeline` before its own ``finally`` can run. The timeout
    branch still reconciles explicitly, because the pumps must reach EOF after
    the stages are terminated but *before* the outputs are gathered;
    :func:`_reconcile_pipe_tasks` is safe to run twice, so the ``finally`` is a
    no-op once that has happened.
    """
    timeout = config.timeout
    timeout_deadline: float | None = None
    if timeout is not None:
        timeout_deadline = time.monotonic() + timeout

    pipe_tasks = _create_pipe_tasks(spawn.processes)
    try:
        wait_result = await _await_pipeline_wait_result(
            spawn,
            config,
            timeout_deadline=timeout_deadline,
            pipe_tasks=pipe_tasks,
        )
        stderr_by_stage, final_stdout = await _gather_pipeline_outputs(spawn)
        return _PipelineStageResultInputs(
            wait_result=wait_result,
            stderr_by_stage=stderr_by_stage,
            final_stdout=final_stdout,
        )
    except TimeoutError as exc:
        await _terminate_timed_out_stages(spawn.processes, config.ctx.cancel_grace)
        await _reconcile_pipe_tasks(pipe_tasks)
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
    finally:
        await _reconcile_pipe_tasks(pipe_tasks)


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


async def _drain_tasks_during_cleanup(
    pending_tasks: list[asyncio.Task[None]],
    active_error: BaseException,
    *,
    message: str,
) -> None:
    """Drain observe tasks in cleanup, aggregating a failure with ``active_error``.

    Shared by the pipeline and single-command cleanup paths. ``message`` is
    required rather than defaulted so a caller cannot silently inherit another
    path's finalization label.
    """
    try:
        await _wait_for_exec_hook_tasks(pending_tasks)
    except BaseException as task_error:  # noqa: BLE001
        # A blind catch is intentional: every task failure, including
        # non-Exception BaseExceptions, must be aggregated with active_error so
        # cleanup never masks the error that triggered it.
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
        await _drain_tasks_during_cleanup(
            pending_tasks, after_hook_error, message=_PIPELINE_FINALIZATION_ERROR
        )
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
        await _drain_tasks_during_cleanup(
            pending_tasks, spawn_error, message=_PIPELINE_FINALIZATION_ERROR
        )
        raise
    try:
        inputs = await _collect_pipeline_inputs(
            parts,
            spawn,
            config,
        )
    except _sh_module().TimeoutExpired as timeout_error:
        _report_pipeline_timeout_expiry(
            observations,
            spawn.processes,
            configured_timeout=config.timeout,
        )
        _emit_timeout_exit_events(observations, spawn)
        await _drain_tasks_during_cleanup(
            pending_tasks, timeout_error, message=_PIPELINE_FINALIZATION_ERROR
        )
        raise
    except BaseException as run_error:
        await _cancel_stream_tasks(spawn.stderr_tasks, spawn.stdout_task)
        await _drain_tasks_during_cleanup(
            pending_tasks, run_error, message=_PIPELINE_FINALIZATION_ERROR
        )
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
