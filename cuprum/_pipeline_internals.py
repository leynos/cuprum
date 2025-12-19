"""Internal pipeline execution coordination and fail-fast semantics."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import sys
import typing as typ

from cuprum._streams import _consume_stream, _pump_stream, _StreamConfig
from cuprum.context import current_context

if typ.TYPE_CHECKING:
    import types

    from cuprum.context import AfterHook
    from cuprum.sh import CommandResult, ExecutionContext, PipelineResult, SafeCmd

_MIN_PIPELINE_STAGES = 2


def _sh_module() -> types.ModuleType:
    module = sys.modules.get("cuprum.sh")
    if module is None:
        msg = "cuprum.sh must be imported before running pipelines"
        raise RuntimeError(msg)
    return module


def _run_before_hooks(cmd: SafeCmd) -> tuple[AfterHook, ...]:
    """Check allowlist, run before hooks, and return after hooks for later.

    This helper validates the command against the current context's allowlist,
    executes all registered before hooks in FIFO order, and returns the after
    hooks tuple for invocation after command completion.
    """
    ctx = current_context()
    ctx.check_allowed(cmd.program)
    for hook in ctx.before_hooks:
        hook(cmd)
    return ctx.after_hooks


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
    grace_period = max(0.0, grace_period)
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(process.wait(), grace_period)
    except asyncio.TimeoutError:  # noqa: UP041 - explicit asyncio timeout needed
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            return
        await process.wait()


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
    after_hooks_by_stage = tuple(_run_before_hooks(cmd) for cmd in parts)
    processes, stderr_tasks, stdout_task = await _spawn_pipeline_processes(
        parts,
        config,
    )
    wait_result = await _wait_for_pipeline(
        processes,
        pipe_tasks=_create_pipe_tasks(processes),
        stream_tasks=_flatten_stream_tasks(stderr_tasks, stdout_task),
        cancel_grace=config.ctx.cancel_grace,
    )

    stderr_by_stage = [None if task is None else await task for task in stderr_tasks]
    final_stdout = None if stdout_task is None else await stdout_task
    stage_results: list[CommandResult] = []
    for idx, cmd in enumerate(parts):
        process = processes[idx]
        stage_results.append(
            sh.CommandResult(
                program=cmd.program,
                argv=cmd.argv,
                exit_code=wait_result.exit_codes[idx],
                pid=process.pid if process.pid is not None else -1,
                stdout=final_stdout if idx == len(parts) - 1 else None,
                stderr=stderr_by_stage[idx],
            ),
        )
    _run_pipeline_after_hooks(parts, after_hooks_by_stage, stage_results)

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
    idx: int,
    last_idx: int,
    config: _PipelineRunConfig,
) -> tuple[asyncio.Task[str | None] | None, asyncio.Task[str | None] | None]:
    """Create stderr and stdout capture tasks for a pipeline stage.

    Returns (stderr_task, stdout_task). stderr is captured for all stages when
    capture_or_echo is enabled. stdout is only captured for the final stage.
    """
    stderr_task: asyncio.Task[str | None] | None = None
    stdout_task: asyncio.Task[str | None] | None = None

    if config.capture_or_echo:
        stderr_task = asyncio.create_task(
            _consume_stream(
                process.stderr,
                dc.replace(config.stream_config, sink=config.stderr_sink),
            ),
        )
        if idx == last_idx:
            stdout_task = asyncio.create_task(
                _consume_stream(process.stdout, config.stream_config),
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


async def _spawn_pipeline_processes(
    parts: tuple[SafeCmd, ...],
    config: _PipelineRunConfig,
) -> tuple[
    list[asyncio.subprocess.Process],
    list[asyncio.Task[str | None] | None],
    asyncio.Task[str | None] | None,
]:
    """Start subprocesses for each stage and wire up capture tasks."""
    processes: list[asyncio.subprocess.Process] = []
    stderr_tasks: list[asyncio.Task[str | None] | None] = []
    stdout_task: asyncio.Task[str | None] | None = None

    last_idx = len(parts) - 1
    try:
        for idx, cmd in enumerate(parts):
            stream_fds = _get_stage_stream_fds(
                idx,
                last_idx,
                capture_or_echo=config.capture_or_echo,
            )

            process = await asyncio.create_subprocess_exec(
                *cmd.argv_with_program,
                stdin=stream_fds.stdin,
                stdout=stream_fds.stdout,
                stderr=stream_fds.stderr,
                env=_merge_env(config.ctx.env),
                cwd=str(config.ctx.cwd) if config.ctx.cwd is not None else None,
            )
            processes.append(process)

            stderr_task, new_stdout_task = _create_stage_capture_tasks(
                process,
                idx,
                last_idx,
                config,
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

    return processes, stderr_tasks, stdout_task


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


async def _cleanup_pipeline_on_error(
    processes: list[asyncio.subprocess.Process],
    pipe_tasks: list[asyncio.Task[None]],
    stream_tasks: list[asyncio.Task[str | None]],
    cancel_grace: float,
) -> list[object]:
    """Clean up pipeline resources after an error or cancellation.

    Terminates all processes, collects pipe task results, and awaits stream
    tasks to ensure orderly shutdown. Returns the collected pipe results for
    use in the finally block.
    """
    await asyncio.gather(
        *(_terminate_process(p, cancel_grace) for p in processes),
        return_exceptions=True,
    )
    pipe_results = await _collect_pipe_results(pipe_tasks)
    await asyncio.gather(*stream_tasks, return_exceptions=True)
    return pipe_results


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
    if state.failure_index is None and exit_code != 0:
        state.failure_index = idx
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
    stream_tasks: list[asyncio.Task[str | None]],
    cancel_grace: float,
) -> _PipelineWaitResult:
    """Wait for pipeline completion, ensuring subprocess cleanup on cancellation."""
    state = _PipelineWaitState.from_processes(processes)

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
        )
    except BaseException as exc:
        caught = exc
        pipe_results = await _cleanup_pipeline_on_error(
            processes,
            pipe_tasks,
            stream_tasks,
            cancel_grace,
        )
        await asyncio.gather(*state.wait_tasks, return_exceptions=True)
        raise
    finally:
        pipe_results = await _finalize_pipeline_wait(pipe_tasks, pipe_results, caught)


@dc.dataclass(frozen=True, slots=True)
class _PipelineWaitResult:
    exit_codes: list[int]
    failure_index: int | None


@dc.dataclass(slots=True)
class _PipelineWaitState:
    wait_tasks: list[asyncio.Task[int]]
    task_to_index: dict[asyncio.Task[int], int]
    exit_codes: list[int | None]
    failure_index: int | None = None

    @classmethod
    def from_processes(
        cls,
        processes: list[asyncio.subprocess.Process],
    ) -> _PipelineWaitState:
        wait_tasks = [asyncio.create_task(process.wait()) for process in processes]
        return cls(
            wait_tasks=wait_tasks,
            task_to_index={task: idx for idx, task in enumerate(wait_tasks)},
            exit_codes=[None] * len(processes),
        )


async def _terminate_process_via_wait_task(
    process: asyncio.subprocess.Process,
    wait_task: asyncio.Task[int],
    grace_period: float,
) -> None:
    """Terminate a process, awaiting the provided wait task for completion."""
    grace_period = max(0.0, grace_period)
    if wait_task.done():
        return
    try:
        process.terminate()
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(asyncio.shield(wait_task), grace_period)
    except asyncio.TimeoutError:  # noqa: UP041 - explicit asyncio timeout needed
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            return
        await asyncio.shield(wait_task)


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
    after_hooks_by_stage: tuple[tuple[AfterHook, ...], ...],
    results: list[CommandResult],
) -> None:
    """Run registered after hooks for each pipeline stage."""
    for cmd, hooks, result in zip(parts, after_hooks_by_stage, results, strict=True):
        for hook in hooks:
            hook(cmd, result)
