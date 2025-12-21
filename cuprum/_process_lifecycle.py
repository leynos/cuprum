"""Process lifecycle management for pipeline execution."""

from __future__ import annotations

import asyncio
import time
import typing as typ
from pathlib import Path

from cuprum._observability import _freeze_str_mapping, _merge_tags
from cuprum._pipeline_streams import _collect_pipe_results

if typ.TYPE_CHECKING:
    from cuprum._pipeline_internals import _StageObservation
    from cuprum._pipeline_streams import _PipelineRunConfig
    from cuprum.sh import SafeCmd


async def _terminate_process(
    process: asyncio.subprocess.Process,
    grace_period: float,
) -> None:
    """Terminate a running process, escalating to kill after the grace period."""
    await _terminate_process_with_wait(
        process,
        grace_period=grace_period,
        is_done=lambda: process.returncode is not None,
        wait_for_exit=process.wait,
    )


async def _terminate_process_with_wait(
    process: asyncio.subprocess.Process,
    *,
    grace_period: float,
    is_done: typ.Callable[[], bool],
    wait_for_exit: typ.Callable[[], typ.Awaitable[int]],
) -> None:
    """Terminate a process, awaiting completion via the provided waiter."""
    grace_period = max(0.0, grace_period)
    if is_done():
        return
    try:
        process.terminate()
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(wait_for_exit(), grace_period)
    except asyncio.TimeoutError:  # noqa: UP041 - explicit asyncio timeout needed
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            return
        await wait_for_exit()


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
    await asyncio.gather(
        *(_terminate_process(p, cancel_grace) for p in processes),
        return_exceptions=True,
    )

    tasks: list[asyncio.Task[str | None]] = [
        task for task in stderr_tasks if task is not None
    ]
    if stdout_task is not None:
        tasks.append(stdout_task)

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _cleanup_pipeline_on_error(
    processes: list[asyncio.subprocess.Process],
    pipe_tasks: list[asyncio.Task[None]],
    cancel_grace: float,
) -> list[object]:
    """Clean up pipeline resources after an error or cancellation.

    Terminates all processes and collects pipe task results. Stream consumer
    tasks are owned by the caller (``_run_pipeline``).

    Returns the collected pipe results for use in the finally block.
    """
    await asyncio.gather(
        *(_terminate_process(p, cancel_grace) for p in processes),
        return_exceptions=True,
    )
    return await _collect_pipe_results(pipe_tasks)


def _merge_env(extra: typ.Mapping[str, str] | None) -> dict[str, str] | None:
    """Overlay extra environment variables when provided."""
    if extra is None:
        return None
    import os

    merged = os.environ.copy()
    merged |= extra
    return merged


def _build_spawn_observations(
    parts: tuple[SafeCmd, ...],
    config: _PipelineRunConfig,
) -> tuple[_StageObservation, ...]:
    from cuprum._pipeline_internals import _run_before_hooks, _StageObservation

    hooks_by_stage = tuple(_run_before_hooks(cmd) for cmd in parts)
    pending_tasks: list[asyncio.Task[None]] = []
    cwd = None if config.ctx.cwd is None else Path(config.ctx.cwd)
    env_overlay = _freeze_str_mapping(config.ctx.env)
    observations = tuple(
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
    if any(obs.hooks.observe_hooks for obs in observations):
        msg = "spawn helpers require explicit observations when observe hooks exist"
        raise RuntimeError(msg)
    return observations


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
]:
    """Start subprocesses for each stage and wire up capture tasks."""
    from cuprum._pipeline_internals import _EventDetails
    from cuprum._pipeline_streams import _create_stage_capture_tasks

    if observations is None:
        observations = _build_spawn_observations(parts, config)

    processes: list[asyncio.subprocess.Process] = []
    stderr_tasks: list[asyncio.Task[str | None] | None] = []
    stdout_task: asyncio.Task[str | None] | None = None
    started_at: list[float] = []

    last_idx = len(observations) - 1
    try:
        for idx, observation in enumerate(observations):
            process = await asyncio.create_subprocess_exec(
                *observation.cmd.argv_with_program,
                stdin=(
                    asyncio.subprocess.DEVNULL if idx == 0 else asyncio.subprocess.PIPE
                ),
                stdout=(
                    asyncio.subprocess.PIPE
                    if idx != last_idx or config.capture_or_echo
                    else asyncio.subprocess.DEVNULL
                ),
                stderr=(
                    asyncio.subprocess.PIPE
                    if config.capture_or_echo
                    else asyncio.subprocess.DEVNULL
                ),
                env=_merge_env(config.ctx.env),
                cwd=str(config.ctx.cwd) if config.ctx.cwd is not None else None,
            )
            processes.append(process)
            started_at.append(time.perf_counter())
            observation.emit("start", _EventDetails(pid=process.pid))

            stderr_task, new_stdout_task = _create_stage_capture_tasks(
                process,
                config,
                is_last_stage=(idx == last_idx),
                observation=observation,
            )
            stderr_tasks.append(stderr_task)
            if new_stdout_task is not None:
                stdout_task = new_stdout_task
    except BaseException:
        await _cleanup_spawned_processes(
            processes,
            stderr_tasks,
            stdout_task,
            config.ctx.cancel_grace,
        )
        raise

    return processes, stderr_tasks, stdout_task, started_at


async def _terminate_process_via_wait_task(
    process: asyncio.subprocess.Process,
    wait_task: asyncio.Task[int],
    grace_period: float,
) -> None:
    """Terminate a process, awaiting the provided wait task for completion."""
    await _terminate_process_with_wait(
        process,
        grace_period=grace_period,
        is_done=wait_task.done,
        wait_for_exit=lambda: asyncio.shield(wait_task),
    )


async def _terminate_pipeline_remaining_stages(
    processes: list[asyncio.subprocess.Process],
    wait_tasks: list[asyncio.Task[int]],
    failure_index: int,
    *,
    cancel_grace: float,
) -> None:
    """Terminate all still-running stages after a stage fails.

    Once a stage exits non-zero, Cuprum applies fail-fast semantics by
    terminating the remaining pipeline stages. This prevents pipelines from
    hanging on long-running producers/consumers when downstream work is no
    longer meaningful.
    """
    termination_tasks: list[asyncio.Task[None]] = []
    for idx, (process, wait_task) in enumerate(
        zip(processes, wait_tasks, strict=True),
    ):
        if idx == failure_index:
            continue
        if wait_task.done():
            continue
        termination_tasks.append(
            asyncio.create_task(
                _terminate_process_via_wait_task(
                    process,
                    wait_task,
                    cancel_grace,
                ),
            ),
        )
    if termination_tasks:
        await asyncio.gather(*termination_tasks, return_exceptions=True)
