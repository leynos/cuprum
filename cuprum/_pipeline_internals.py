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
    _without_env_mode_tag,
)
from cuprum._pipeline_collect import (
    _await_pipeline_wait_result,
    _build_timeout_expired_error,
    _collect_pipeline_inputs,
    _gather_pipeline_outputs,
    _sh_module,
)
from cuprum._pipeline_finalize import (
    _PIPELINE_FINALIZATION_ERROR,
    _finalize_pipeline_execution,
    _finalize_pipeline_timeout,
    _reconcile_pipeline_run_failure,
    _run_pipeline_after_hooks,
)
from cuprum._pipeline_results import (
    _build_pipeline_result,
    _build_pipeline_stage_results,
)
from cuprum._pipeline_spawn import _spawn_pipeline_processes
from cuprum._pipeline_types import (
    _EventDetails,
    _ExecutionHooks,
    _PipelineObservers,
    _PipelineSpawnResult,
    _StageObservation,
    _StageWaitContext,
)
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._result_types import _AnyPipelineResult
from cuprum._sink_lifecycle import _outcome_for_error
from cuprum.context import EnvMode, current_context

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._pipeline_config import _PipelineRunConfig
    from cuprum.context import CuprumContext
    from cuprum.sh import PipelineResult, SafeCmd

# Every name the finalization module owns is re-exported here, because this is
# the module ``cuprum.sh`` and the internal callers already import those helpers
# from. Having moved the implementations is a private arrangement; making
# callers follow it would be churn with no behaviour behind it.
__all__ = [
    "_PIPELINE_FINALIZATION_ERROR",
    "_await_pipeline_wait_result",
    "_build_pipeline_result",
    "_build_timeout_expired_error",
    "_collect_pipeline_inputs",
    "_finalize_pipeline_execution",
    "_finalize_pipeline_timeout",
    "_gather_pipeline_outputs",
    "_reconcile_pipeline_run_failure",
    "_run_pipeline_after_hooks",
    "_sh_module",
]

_MIN_PIPELINE_STAGES = 2


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
    env_overlay, env_mode = _resolve_env_overlay(config.ctx.env, config.ctx.env_mode)
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
                _without_env_mode_tag(config.ctx.tags),
                {"env_mode": env_mode} if env_mode is EnvMode.REPLACE else None,
            ),
            cwd=cwd,
            env_overlay=env_overlay,
            pending_tasks=pending_tasks,
            wall_clock=time.time,
            env_mode=env_mode,
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


async def _run_spawned_pipeline(
    parts: tuple[SafeCmd, ...],
    config: _PipelineRunConfig,
    spawn: _PipelineSpawnResult,
    observers: _PipelineObservers,
) -> _AnyPipelineResult:
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
    PipelineResult | BytesPipelineResult
        The assembled stage results and the index of the first failing stage,
        byte-exact when the pipeline was asked for bytes.
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
            observations,
            processes=spawn.processes,
            inputs=inputs,
            capture_bytes=config.capture_bytes,
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

    return _build_pipeline_result(
        stage_results,
        failure_index=inputs.wait_result.failure_index,
        capture_bytes=config.capture_bytes,
    )


async def _run_pipeline(
    parts: tuple[SafeCmd, ...],
    config: _PipelineRunConfig,
) -> _AnyPipelineResult:
    """Execute a pipeline and return a structured result.

    A thin wrapper, so that the aggregate idle heartbeat is settled on every
    exit path without threading a ``finally`` through the spawn and drive
    halves below.

    Returns
    -------
    PipelineResult | BytesPipelineResult
        The assembled stage results and the index of the first failing stage,
        byte-exact when the pipeline was asked for bytes.
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
) -> _AnyPipelineResult:
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
            wall_clock_started_at,
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
                wall_clock_started_at=tuple(wall_clock_started_at),
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
