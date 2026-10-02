"""Pipeline stage spawning and the resources a partial spawn leaves behind.

``_spawn_pipeline_processes`` starts every stage in turn, wiring each one's
stdio handles and capture tasks as it goes, and returns the accumulated
resources to the caller that waits on, collects, and tears down the pipeline.
Spawning is the one moment when a failure can leave *half* a pipeline running,
so the resources are accumulated in ``_SpawnedPipelineStages`` rather than
returned incrementally: ``_spawn_pipeline_processes`` can then hand that
partially-filled accumulator to ``_cleanup_spawned_processes`` before the
failure propagates, losing neither a started process nor a capture task.

The stdio policy and capture-task construction are not decided here.
``cuprum._pipeline_stage_streams`` owns both; this module sequences them.
Termination itself lives in ``cuprum._process_lifecycle``, next to the
cancellation-safe cleanup primitive that the teardown paths share with
single-command execution. Whether a pipeline should stop early is a separate
decision, taken by ``cuprum._pipeline_wait``.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import time
import typing as typ

from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._pipeline_stage_streams import _get_stage_stream_fds
from cuprum._pipeline_types import _EventDetails, _StageObservation
from cuprum._process_lifecycle import _merge_env, _terminate_all_shielded
from cuprum._subprocess_context import _cwd_arg

if typ.TYPE_CHECKING:
    from cuprum._pipeline_config import _PipelineRunConfig
    from cuprum._streams import _RelayDiagnostics
    from cuprum.sh import SafeCmd

__all__ = ["_spawn_pipeline_processes"]


def _build_spawn_observations(
    parts: tuple[SafeCmd, ...],
    config: _PipelineRunConfig,
) -> tuple[_StageObservation, ...]:
    """Build per-stage observation state for spawning a pipeline."""
    from cuprum._pipeline_internals import _build_pipeline_observations

    observations = _build_pipeline_observations(parts, config, pending_tasks=[])
    # The pending-task list built here is discarded, so observe hooks (which
    # rely on it) cannot run on the spawn path; callers must supply explicit
    # observations instead.
    if any(obs.hooks.observe_hooks for obs in observations):
        msg = "spawn helpers require explicit observations when observe hooks exist"
        raise RuntimeError(msg)
    return observations


@dc.dataclass(slots=True)
class _SpawnedPipelineStages:
    """Resources accumulated while spawning pipeline stages."""

    processes: list[asyncio.subprocess.Process] = dc.field(default_factory=list)
    stderr_tasks: list[asyncio.Task[str | None] | None] = dc.field(default_factory=list)
    stdout_task: asyncio.Task[str | None] | None = None
    started_at: list[float] = dc.field(default_factory=list)
    wall_clock_started_at: list[float] = dc.field(default_factory=list)
    relay_diagnostics_by_stage: list[
        tuple[_RelayDiagnostics | None, _RelayDiagnostics | None]
    ] = dc.field(default_factory=list)


async def _spawn_pipeline_stages(
    resources: _SpawnedPipelineStages,
    observations: tuple[_StageObservation, ...],
    config: _PipelineRunConfig,
) -> None:
    """Spawn stages and accumulate their runtime resources."""
    from cuprum._pipeline_stage_streams import (
        _create_stage_capture_tasks,
        _StageCaptureRequest,
    )

    last_idx = len(observations) - 1
    for idx, observation in enumerate(observations):
        # Both clocks are sampled before the spawn await, so a stage's
        # recorded duration includes the time its spawn blocked, matching
        # the direct-command boundary. Sampling after the await would
        # exclude the spawn-await latency instead.
        resources.started_at.append(time.perf_counter())
        resources.wall_clock_started_at.append(observation.wall_clock())
        stream_fds = _get_stage_stream_fds(
            idx,
            last_idx,
            consumes_stdout=config.consumes_stdout,
            consumes_stderr=config.consumes_stderr,
        )
        process = await asyncio.create_subprocess_exec(
            *observation.cmd.argv_with_program,
            stdin=stream_fds.stdin,
            stdout=stream_fds.stdout,
            stderr=stream_fds.stderr,
            env=_merge_env(config.ctx.env, config.ctx.env_mode),
            cwd=_cwd_arg(config.ctx.cwd),
        )
        resources.processes.append(process)
        observation.emit("start", _EventDetails(pid=process.pid))
        if idx == 0 and config.idle is not None:
            # The aggregate clock starts with the first stage actually
            # running, so the plan events and before hooks that preceded it
            # are not mistaken for pipeline silence.
            config.idle.launch()

        stage_tasks = _create_stage_capture_tasks(
            _StageCaptureRequest(
                process=process,
                config=config,
                observation=observation,
                is_last_stage=(idx == last_idx),
                started_at=resources.started_at[-1],
            ),
        )
        resources.stderr_tasks.append(stage_tasks[0])
        resources.relay_diagnostics_by_stage.append(stage_tasks[2])
        if stage_tasks[1] is not None:
            resources.stdout_task = stage_tasks[1]


async def _cleanup_spawned_processes(
    processes: list[asyncio.subprocess.Process],
    stderr_tasks: list[asyncio.Task[str | None] | None],
    stdout_task: asyncio.Task[str | None] | None,
    cancel_grace: float,
) -> None:
    """Terminate processes and cancel tasks after a spawn failure.

    Terminates all started processes and cancels any capture tasks to prevent
    resource leaks when a pipeline stage fails to spawn.
    """
    await _terminate_all_shielded(processes, cancel_grace)

    tasks: list[asyncio.Task[str | None]] = [
        task for task in stderr_tasks if task is not None
    ]
    if stdout_task is not None:
        tasks.append(stdout_task)

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _spawn_pipeline_processes(
    parts: tuple[SafeCmd, ...],
    config: _PipelineRunConfig,
    *,
    observations: tuple[_StageObservation, ...] | None = None,
) -> tuple[
    list[asyncio.subprocess.Process],
    list[asyncio.Task[str | None] | None],
    asyncio.Task[str | None] | None,
    list[float],
    list[float],
    list[tuple[_RelayDiagnostics | None, _RelayDiagnostics | None]],
]:
    """Start subprocesses and wire up their capture tasks."""
    if observations is None:
        observations = _build_spawn_observations(parts, config)

    resources = _SpawnedPipelineStages()
    try:
        await _spawn_pipeline_stages(resources, observations, config)
    except BaseException:
        # Teardown begins here, so the heartbeat stops here: it must not narrate
        # a pipeline that is already being torn down.
        await _stop_idle_monitor(config.idle)
        await _cleanup_spawned_processes(
            resources.processes,
            resources.stderr_tasks,
            resources.stdout_task,
            config.ctx.cancel_grace,
        )
        raise

    return (
        resources.processes,
        resources.stderr_tasks,
        resources.stdout_task,
        resources.started_at,
        resources.wall_clock_started_at,
        resources.relay_diagnostics_by_stage,
    )
