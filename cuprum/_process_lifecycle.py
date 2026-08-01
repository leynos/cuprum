"""Process lifecycle management for pipeline execution."""
from __future__ import annotations

import asyncio
import contextlib
import time
import typing as typ

from cuprum._pipeline_stage_streams import _get_stage_stream_fds
from cuprum._pipeline_streams import _collect_pipe_results
from cuprum._pipeline_types import _EventDetails, _StageObservation
from cuprum._process_exit import _await_process_exit
from cuprum._subprocess_context import _cwd_arg
from cuprum.context import current_context, resolve_env

_PROCESS_EXIT_POLL_INTERVAL = 0.01
if typ.TYPE_CHECKING:
    import collections.abc as cabc
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
        wait_for_exit=lambda: _await_process_exit(process),
    )


async def _terminate_process_with_wait(
    process: asyncio.subprocess.Process,
    *,
    grace_period: float,
    is_done: cabc.Callable[[], bool],
    wait_for_exit: cabc.Callable[[], cabc.Awaitable[int]],
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


async def _await_teardown_shielded(
    teardowns: cabc.Iterable[cabc.Awaitable[object]],
) -> None:
    """Run ``teardowns`` to completion, holding off caller cancellation.

    Teardown runs in an owned, shielded task. Awaiting it directly would let a
    caller cancelling the run interrupt it part-way: ``process.terminate()`` is
    synchronous and would still have fired, but the cancellation lands on the
    grace-period wait, so the ``SIGKILL`` escalation and the final reap never
    run and a process ignoring ``SIGTERM`` survives the run that spawned it.

    The cancellation is held until teardown finishes and then re-raised,
    mirroring the ``asyncio.shield`` cleanup in
    ``cuprum.sh._execute_with_hooks``. A second cancellation arriving during
    that wait is suppressed rather than allowed to strand a process: the
    teardown task is independent and completes regardless.

    Failures are absorbed — every caller runs this while another exception is
    already propagating, and teardown must not replace it.

    Raises
    ------
    asyncio.CancelledError
        Re-raised after the shielded teardown completes, once the initial
        cancellation that interrupted the shield has been held off.
    """
    termination = asyncio.ensure_future(
        asyncio.gather(*teardowns, return_exceptions=True)
    )
    try:
        await asyncio.shield(termination)
    except asyncio.CancelledError:
        with contextlib.suppress(asyncio.CancelledError):
            await termination
        raise


async def _terminate_all_shielded(
    processes: cabc.Iterable[asyncio.subprocess.Process],
    cancel_grace: float,
) -> None:
    """Terminate every process, holding off caller cancellation until done.

    Termination owns its own task and is shielded. Awaiting it directly would
    let a caller cancelling the run interrupt teardown: ``process.terminate()``
    is synchronous and would still have fired, but the cancellation lands on
    the grace-period wait, so the ``SIGKILL`` escalation and the final reap
    never run and a process ignoring ``SIGTERM`` survives the run that spawned
    it.

    The cancellation is held until termination finishes and then re-raised,
    mirroring the ``asyncio.shield`` cleanup in
    ``cuprum.sh._execute_with_hooks``. A second cancellation arriving during
    that wait is suppressed rather than allowed to strand a process: the
    termination task is independent and completes regardless.

    Failures are absorbed — every caller runs this while another exception is
    already propagating, and teardown must not replace it.
    """
    await _await_teardown_shielded(
        _terminate_process(process, cancel_grace) for process in processes
    )


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


async def _cleanup_pipeline_on_error(
    processes: list[asyncio.subprocess.Process],
    pipe_tasks: list[asyncio.Task[None]],
    cancel_grace: float,
) -> list[object]:
    """Clean up pipeline resources after an error or cancellation."""
    # Terminates every process and collects the pipe task results, which are
    # returned for the caller's ``finally`` block. Stream consumer tasks are
    # owned by the caller (``_run_pipeline``), not by this helper.
    await _terminate_all_shielded(processes, cancel_grace)
    return await _collect_pipe_results(pipe_tasks)


def _merge_env(
    extra: cabc.Mapping[str, str] | None,
    *,
    include_context_overlay: bool = True,
) -> dict[str, str] | None:
    """Overlay environment variables on the live :func:`os.environ`."""
    overlay = current_context().env_overlay if include_context_overlay else None
    return resolve_env(overlay, extra)


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
            stream_fds = _get_stage_stream_fds(
                idx,
                last_idx,
                capture_or_echo=config.capture_or_echo,
            )
            process = await asyncio.create_subprocess_exec(
                *observation.cmd.argv_with_program,
                stdin=stream_fds.stdin,
                stdout=stream_fds.stdout,
                stderr=stream_fds.stderr,
                env=_merge_env(config.ctx.env),
                cwd=_cwd_arg(config.ctx.cwd),
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


def _stages_to_terminate(
    failure_index: int,
    done: cabc.Sequence[bool],
) -> list[int]:
    """Return the stage indices to terminate after a fail-fast, each once."""
    # This is the pure selection behind _terminate_pipeline_remaining_stages.
    # Every stage is scheduled at most once — indices come from a single
    # enumeration — and two stages are never scheduled: the failed stage, which
    # owns its own exit, and any already-finished stage, which needs no
    # termination. Cleanup is therefore idempotent: a second pass over settled
    # stages (all ``done``) selects nothing.
    return [
        idx for idx, is_done in enumerate(done) if idx != failure_index and not is_done
    ]


async def _terminate_timed_out_stages(
    processes: cabc.Sequence[asyncio.subprocess.Process],
    cancel_grace: float,
) -> None:
    """Terminate every still-running stage after a pipeline deadline expires.

    ``_wait_for_pipeline`` normally performs this teardown itself when the
    deadline cancels it. A non-positive deadline gives ``asyncio.wait_for`` a
    zero timeout, and it then cancels that coroutine before it ever runs, so
    nothing terminates the stages and they are left orphaned for the rest of
    their natural life. Terminating here covers that route.

    Running on both routes is safe and deliberate: ``_terminate_process``
    returns immediately for a stage that has already exited. Doing it before
    the output gather also lets the stage pipes reach EOF, so a captured
    pipeline cannot block waiting on a producer that is still running.

    Failures are absorbed — this runs while a timeout is already propagating,
    and must not replace the ``TimeoutExpired`` the caller is waiting for.

    Teardown owns its own task and is shielded from the caller. Awaiting the
    terminations directly would let a caller cancelling ``Pipeline.run()``
    interrupt them: ``process.terminate()`` is synchronous and would still have
    fired, but the cancellation lands on the grace-period wait, so the
    ``SIGKILL`` escalation and the final reap never run and a stage ignoring
    ``SIGTERM`` survives. The cancellation is therefore held until termination
    has finished, mirroring the ``asyncio.shield`` cleanup in
    ``cuprum.sh._execute_with_hooks``.
    """
    await _terminate_all_shielded(processes, cancel_grace)


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

    Teardown is shielded for the same reason as the timeout path: a caller
    cancelling mid-grace must not strand a stage before its ``SIGKILL``
    escalation runs.
    """
    targets = set(
        _stages_to_terminate(
            failure_index,
            [wait_task.done() for wait_task in wait_tasks],
        )
    )
    termination_tasks = [
        asyncio.create_task(
            _terminate_process_via_wait_task(process, wait_task, cancel_grace),
        )
        for idx, (process, wait_task) in enumerate(
            zip(processes, wait_tasks, strict=True),
        )
        if idx in targets
    ]
    if termination_tasks:
        await _await_teardown_shielded(termination_tasks)


async def _await_process_exit(process: asyncio.subprocess.Process) -> int:
    """Return the process exit code without trusting a stranded wait future."""
    if process.returncode is not None:
        return process.returncode

    async def _published_returncode() -> int:
        """Poll the authoritative return code at a low frequency."""
        while process.returncode is None:
            await asyncio.sleep(_PROCESS_EXIT_POLL_INTERVAL)
        return process.returncode

    wait_task = asyncio.create_task(process.wait())
    published_task = asyncio.create_task(_published_returncode())
    try:
        completed, _ = await asyncio.wait(
            (wait_task, published_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if wait_task in completed:
            return wait_task.result()
        return published_task.result()
    finally:
        for task in (wait_task, published_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(wait_task, published_task, return_exceptions=True)
