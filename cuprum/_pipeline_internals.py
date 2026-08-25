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

import time
import typing as typ
from pathlib import Path

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
from cuprum._pipeline_streams import (
    _cancel_stream_tasks,
    _PipelineRunConfig,
)
from cuprum._pipeline_types import (
    _EventDetails,
    _ExecutionHooks,
    _PipelineObservers,
    _PipelineSpawnResult,
    _StageObservation,
    _StageWaitContext,
)
from cuprum._process_lifecycle import _shielded_cleanup, _spawn_pipeline_processes
from cuprum._timeout_reporting import _report_pipeline_timeout_expiry
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
    observations: tuple[_StageObservation, ...],
    stage_results: list[CommandResult],
    pending_tasks: list[asyncio.Task[None]],
) -> None:
    """Run after hooks for every stage and drain pending observe tasks.

    Both drains are shielded. The pipeline owns these observe-hook tasks, so a
    cancellation landing while finalization waits on them must not return
    before they have settled — that would leak a task per pending hook.
    """
    hooks_by_stage = tuple(obs.hooks for obs in observations)
    try:
        _run_pipeline_after_hooks(parts, hooks_by_stage, stage_results)
    except BaseException as after_hook_error:
        await _shielded_cleanup(
            _drain_tasks_during_cleanup(
                pending_tasks, after_hook_error, message=_PIPELINE_FINALIZATION_ERROR
            )
        )
        raise
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
        await _shielded_cleanup(
            _drain_tasks_during_cleanup(
                pending_tasks, timeout_error, message=_PIPELINE_FINALIZATION_ERROR
            )
        )
        raise
    except BaseException as run_error:
        # One shielded unit: shielding the two separately would let a
        # cancellation landing between them abandon the observe-hook drain.
        await _shielded_cleanup(
            _reconcile_pipeline_run_failure(spawn, pending_tasks, run_error)
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
            stages=_StageWaitContext(
                started_at=tuple(started_at),
                observations=observations,
            ),
        )
    except BaseException as spawn_error:
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
