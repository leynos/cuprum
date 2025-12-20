"""Internal pipeline execution coordination and fail-fast semantics."""

from __future__ import annotations

import asyncio
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
from cuprum._streams import _consume_stream, _pump_stream, _StreamConfig
from cuprum.context import current_context
from cuprum.events import ExecEvent

if typ.TYPE_CHECKING:
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


def _merge_env(extra: typ.Mapping[str, str] | None) -> dict[str, str] | None:
    """Overlay extra environment variables when provided."""
    if extra is None:
        return None
    import os

    merged = os.environ.copy()
    merged |= extra
    return merged


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


@dc.dataclass(frozen=True, slots=True)
class _PipelineRunConfig:
    ctx: ExecutionContext
    capture: bool
    echo: bool
    stdout_sink: typ.IO[str]
    stderr_sink: typ.IO[str]

    @property
    def capture_or_echo(self) -> bool:
        return self.capture or self.echo

    @property
    def stream_config(self) -> _StreamConfig:
        return _StreamConfig(
            capture_output=self.capture,
            echo_output=self.echo,
            sink=self.stdout_sink,
            encoding=self.ctx.encoding,
            errors=self.ctx.errors,
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


def _prepare_pipeline_config(
    *,
    capture: bool,
    echo: bool,
    context: ExecutionContext | None,
) -> _PipelineRunConfig:
    """Normalise runtime options for pipeline execution."""
    sh = _sh_module()
    ctx = context or sh.ExecutionContext()
    stdout_sink = ctx.stdout_sink if ctx.stdout_sink is not None else sys.stdout
    stderr_sink = ctx.stderr_sink if ctx.stderr_sink is not None else sys.stderr
    return _PipelineRunConfig(
        ctx=ctx,
        capture=capture,
        echo=echo,
        stdout_sink=stdout_sink,
        stderr_sink=stderr_sink,
    )


async def _run_pipeline(  # noqa: PLR0914
    parts: tuple[SafeCmd, ...],
    *,
    capture: bool,
    echo: bool,
    context: ExecutionContext | None,
) -> PipelineResult:
    """Execute a pipeline and return a structured result."""
    sh = _sh_module()
    config = _prepare_pipeline_config(capture=capture, echo=echo, context=context)
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
                    "capture": capture,
                    "echo": echo,
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
    try:
        for obs in observations:
            obs.emit("plan", _EventDetails(pid=None))
            for hook in obs.hooks.before_hooks:
                hook(obs.cmd)

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
    pipe_tasks = _create_pipe_tasks(processes)
    stream_tasks = _flatten_stream_tasks(stderr_tasks, stdout_task)
    try:
        wait_result = await _wait_for_pipeline(
            processes,
            pipe_tasks=pipe_tasks,
            cancel_grace=config.ctx.cancel_grace,
            started_at=started_at,
        )
        stderr_by_stage = [
            None if task is None else await task for task in stderr_tasks
        ]
        final_stdout = None if stdout_task is None else await stdout_task
    except BaseException:
        for task in stream_tasks:
            task.cancel()
        await asyncio.gather(*stream_tasks, return_exceptions=True)
        await _wait_for_exec_hook_tasks(pending_tasks)
        raise
    stage_results: list[CommandResult] = []
    for idx, obs in enumerate(observations):
        process = processes[idx]
        ended_at = wait_result.ended_at[idx]
        duration_s = (
            None
            if ended_at is None
            else max(0.0, ended_at - wait_result.started_at[idx])
        )
        obs.emit(
            "exit",
            _EventDetails(
                pid=process.pid,
                exit_code=wait_result.exit_codes[idx],
                duration_s=duration_s,
            ),
        )
        stage_results.append(
            sh.CommandResult(
                program=obs.cmd.program,
                argv=obs.cmd.argv,
                exit_code=wait_result.exit_codes[idx],
                pid=process.pid if process.pid is not None else -1,
                stdout=final_stdout if idx == len(parts) - 1 else None,
                stderr=stderr_by_stage[idx],
            ),
        )
    _run_pipeline_after_hooks(parts, hooks_by_stage, stage_results)
    await _wait_for_exec_hook_tasks(pending_tasks)

    return sh.PipelineResult(
        stages=tuple(stage_results),
        failure_index=wait_result.failure_index,
    )


@dc.dataclass(frozen=True, slots=True)
class _StageStreamConfig:
    """Stream file descriptor configuration for a pipeline stage."""

    stdin: int
    stdout: int
    stderr: int


def _get_stage_stream_fds(
    idx: int,
    last_idx: int,
    *,
    capture_or_echo: bool,
) -> _StageStreamConfig:
    """Determine stream file descriptors for a pipeline stage.

    First stage reads from DEVNULL; intermediate stages use pipes for stdin.
    stdout is piped for intermediate stages or when capturing. stderr is piped
    only when capturing or echoing.
    """
    stdin = asyncio.subprocess.DEVNULL if idx == 0 else asyncio.subprocess.PIPE
    stdout = (
        asyncio.subprocess.PIPE
        if idx != last_idx or capture_or_echo
        else asyncio.subprocess.DEVNULL
    )
    stderr = asyncio.subprocess.PIPE if capture_or_echo else asyncio.subprocess.DEVNULL
    return _StageStreamConfig(stdin=stdin, stdout=stdout, stderr=stderr)


def _create_stage_capture_tasks(
    process: asyncio.subprocess.Process,
    config: _PipelineRunConfig,
    *,
    is_last_stage: bool,
    observation: _StageObservation,
) -> tuple[asyncio.Task[str | None] | None, asyncio.Task[str | None] | None]:
    """Create stderr and stdout capture tasks for a pipeline stage.

    Returns (stderr_task, stdout_task). stderr is captured for all stages when
    capture_or_echo is enabled. stdout is only captured for the final stage.
    """
    stderr_task: asyncio.Task[str | None] | None = None
    stdout_task: asyncio.Task[str | None] | None = None

    if not config.capture_or_echo:
        return stderr_task, stdout_task

    stderr_on_line: typ.Callable[[str], None] | None = None
    if observation.hooks.observe_hooks:

        def stderr_on_line(line: str) -> None:
            observation.emit(
                "stderr",
                _EventDetails(pid=process.pid, line=line),
            )

    stderr_task = asyncio.create_task(
        _consume_stream(
            process.stderr,
            dc.replace(config.stream_config, sink=config.stderr_sink),
            on_line=stderr_on_line,
        ),
    )

    if not is_last_stage:
        return stderr_task, stdout_task

    stdout_on_line: typ.Callable[[str], None] | None = None
    if observation.hooks.observe_hooks:

        def stdout_on_line(line: str) -> None:
            observation.emit(
                "stdout",
                _EventDetails(pid=process.pid, line=line),
            )

    stdout_task = asyncio.create_task(
        _consume_stream(
            process.stdout,
            config.stream_config,
            on_line=stdout_on_line,
        ),
    )

    return stderr_task, stdout_task


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

    tasks: list[asyncio.Task[typ.Any]] = []
    for task in stderr_tasks:
        if task is not None:
            task.cancel()
            tasks.append(task)
    if stdout_task is not None:
        stdout_task.cancel()
        tasks.append(stdout_task)
    await asyncio.gather(*tasks, return_exceptions=True)


def _build_spawn_observations(
    parts: tuple[SafeCmd, ...],
    config: _PipelineRunConfig,
) -> tuple[_StageObservation, ...]:
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
    _parts: tuple[SafeCmd, ...],
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
    if observations is None:
        observations = _build_spawn_observations(_parts, config)

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


def _create_pipe_tasks(
    processes: list[asyncio.subprocess.Process],
) -> list[asyncio.Task[None]]:
    """Create streaming tasks between adjacent pipeline stages."""
    return [
        asyncio.create_task(
            _pump_stream(
                processes[idx].stdout,
                processes[idx + 1].stdin,
            ),
        )
        for idx in range(len(processes) - 1)
    ]


def _flatten_stream_tasks(
    stderr_tasks: list[asyncio.Task[str | None] | None],
    stdout_task: asyncio.Task[str | None] | None,
) -> list[asyncio.Task[str | None]]:
    """Collect all running stream consumer tasks for cancellation cleanup."""
    tasks = [task for task in stderr_tasks if task is not None]
    if stdout_task is not None:
        tasks.append(stdout_task)
    return tasks


async def _collect_pipe_results(
    pipe_tasks: list[asyncio.Task[None]],
) -> list[object]:
    """Collect pipe task results, capturing exceptions rather than raising them.

    Uses return_exceptions=True to gather all results including any exceptions
    that occurred during pipe streaming between pipeline stages.
    """
    return list(await asyncio.gather(*pipe_tasks, return_exceptions=True))


def _surface_unexpected_pipe_failures(pipe_results: list[object]) -> None:
    """Raise non-BrokenPipe exceptions from pipe results.

    BrokenPipeError and ConnectionResetError are expected when downstream
    processes terminate early (e.g., head) and should not fail the pipeline.
    Other exceptions indicate genuine failures and must be surfaced.
    """
    for result in pipe_results:
        if isinstance(result, Exception) and not isinstance(
            result,
            (BrokenPipeError, ConnectionResetError),
        ):
            raise result


@dc.dataclass(frozen=True, slots=True)
class _PipelineWaitResult:
    exit_codes: list[int]
    failure_index: int | None
    started_at: list[float]
    ended_at: list[float | None]


@dc.dataclass(slots=True)
class _PipelineWaitState:
    wait_tasks: list[asyncio.Task[int]]
    task_to_index: dict[asyncio.Task[int], int]
    exit_codes: list[int | None]
    started_at: list[float]
    ended_at: list[float | None]
    failure_index: int | None = None

    @classmethod
    def from_processes(
        cls,
        processes: list[asyncio.subprocess.Process],
        *,
        started_at: list[float],
    ) -> _PipelineWaitState:
        wait_tasks = [asyncio.create_task(process.wait()) for process in processes]
        return cls(
            wait_tasks=wait_tasks,
            task_to_index={task: idx for idx, task in enumerate(wait_tasks)},
            exit_codes=[None] * len(processes),
            started_at=started_at,
            ended_at=[None] * len(processes),
        )


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


async def _process_completed_task(
    task: asyncio.Task[int],
    state: _PipelineWaitState,
    processes: list[asyncio.subprocess.Process],
    cancel_grace: float,
) -> None:
    """Process a completed wait task, terminating remaining stages on failure."""
    idx = state.task_to_index[task]
    exit_code = task.result()
    state.exit_codes[idx] = exit_code
    state.ended_at[idx] = time.perf_counter()
    if state.failure_index is None and exit_code != 0:
        state.failure_index = idx
        if idx != len(processes) - 1:
            await _terminate_pipeline_remaining_stages(
                processes,
                state.wait_tasks,
                idx,
                cancel_grace=cancel_grace,
            )


async def _finalize_pipeline_wait(
    pipe_tasks: list[asyncio.Task[None]],
    pipe_results: list[object] | None,
    caught: BaseException | None,
) -> list[object]:
    """Collect pipe results and surface unexpected failures when appropriate."""
    if pipe_results is None:
        pipe_results = await _collect_pipe_results(pipe_tasks)
    if caught is None:
        _surface_unexpected_pipe_failures(pipe_results)
    return pipe_results


async def _wait_for_pipeline(
    processes: list[asyncio.subprocess.Process],
    *,
    pipe_tasks: list[asyncio.Task[None]],
    cancel_grace: float,
    started_at: list[float],
) -> _PipelineWaitResult:
    """Wait for pipeline completion, ensuring subprocess cleanup on cancellation."""
    state = _PipelineWaitState.from_processes(processes, started_at=started_at)

    caught: BaseException | None = None
    pipe_results: list[object] | None = None
    try:
        pending = set(state.wait_tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in done:
                await _process_completed_task(
                    task,
                    state,
                    processes,
                    cancel_grace,
                )

        completed_exit_codes = [
            -1 if code is None else code for code in state.exit_codes
        ]
        return _PipelineWaitResult(
            exit_codes=completed_exit_codes,
            failure_index=state.failure_index,
            started_at=state.started_at,
            ended_at=state.ended_at,
        )
    except BaseException as exc:
        caught = exc
        pipe_results = await _cleanup_pipeline_on_error(
            processes,
            pipe_tasks,
            cancel_grace,
        )
        await asyncio.gather(*state.wait_tasks, return_exceptions=True)
        raise
    finally:
        pipe_results = await _finalize_pipeline_wait(pipe_tasks, pipe_results, caught)


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


def _run_pipeline_after_hooks(
    parts: tuple[SafeCmd, ...],
    hooks_by_stage: tuple[_ExecutionHooks, ...],
    results: list[CommandResult],
) -> None:
    """Run registered after hooks for each pipeline stage."""
    for cmd, hooks, result in zip(parts, hooks_by_stage, results, strict=True):
        for hook in hooks.after_hooks:
            hook(cmd, result)
