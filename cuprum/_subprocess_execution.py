"""Internal subprocess execution machinery.

This module encapsulates the low-level subprocess spawning, stream handling,
timeout management, and execution lifecycle for SafeCmd.run().
"""
# TODO: refactor into smaller submodules (stdin/runner), see issue #30.
# pylint: disable=too-many-lines

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import sys
import time
import typing as typ

from cuprum._pipeline_internals import _EventDetails, _StageObservation
from cuprum._process_lifecycle import _merge_env, _terminate_process
from cuprum._streams import _consume_stream, _StreamConfig
from cuprum._subprocess_context import _resolve_timeout, _sh_module

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import CommandResult, ExecutionContext, SafeCmd


async def _wait_for_exit_code(
    process: asyncio.subprocess.Process,
    ctx: ExecutionContext,
    *,
    timeout: float | None = None,
    consumers: tuple[asyncio.Task[typ.Any], ...] = (),
) -> tuple[int, float]:
    """Wait for a subprocess, handling cancellation and capturing exit time."""
    try:
        if timeout is None:
            exit_code = await process.wait()
        else:
            exit_code = await asyncio.wait_for(process.wait(), timeout)
    except TimeoutError:  # Raised by asyncio.wait_for on timeout expiry
        await _terminate_process(process, ctx.cancel_grace)
        if consumers:
            await asyncio.gather(*consumers, return_exceptions=True)
        raise
    except asyncio.CancelledError:
        await _terminate_process(process, ctx.cancel_grace)
        if consumers:
            await asyncio.gather(*consumers, return_exceptions=True)
        raise
    exited_at = time.perf_counter()
    return exit_code, exited_at


@dc.dataclass(frozen=True, slots=True)
class _SubprocessExecution:
    """Execution context bundle for subprocess spawning."""

    cmd: SafeCmd
    ctx: ExecutionContext
    capture: bool
    echo: bool
    timeout: float | None
    observation: _StageObservation
    stdin_data: bytes | None


@dc.dataclass(frozen=True, slots=True)
class _SubprocessTimeoutDetails:
    """Captured subprocess timeout details."""

    timeout: float
    stdout: str | None
    stderr: str | None
    exited_at: float


class _SubprocessTimeoutError(Exception):
    """Internal wrapper for subprocess timeouts with captured output."""

    def __init__(self, details: _SubprocessTimeoutDetails) -> None:
        super().__init__(f"Execution exceeded {details.timeout}s timeout")
        self.timeout = details.timeout
        self.stdout = details.stdout
        self.stderr = details.stderr
        self.exited_at = details.exited_at


async def _spawn_subprocess(
    execution: _SubprocessExecution,
) -> asyncio.subprocess.Process:
    """Spawn an async subprocess with configured I/O and environment."""
    return await asyncio.create_subprocess_exec(
        *execution.cmd.argv_with_program,
        stdout=(
            asyncio.subprocess.PIPE
            if execution.capture or execution.echo
            else asyncio.subprocess.DEVNULL
        ),
        stderr=(
            asyncio.subprocess.PIPE
            if execution.capture or execution.echo
            else asyncio.subprocess.DEVNULL
        ),
        stdin=(asyncio.subprocess.PIPE if execution.stdin_data is not None else None),
        env=_merge_env(execution.ctx.env),
        cwd=(str(execution.ctx.cwd) if execution.ctx.cwd is not None else None),
    )


async def _write_stdin(
    process: asyncio.subprocess.Process,
    stdin_data: bytes,
    observation: _StageObservation,
) -> None:
    """Write caller-provided stdin bytes and close the pipe."""
    stdin = process.stdin
    if stdin is None:
        return
    try:
        stdin.write(stdin_data)
        await stdin.drain()
    except (BrokenPipeError, ConnectionResetError) as exc:
        observation.emit(
            "stdin_error",
            _EventDetails(
                pid=process.pid,
                note=f"{type(exc).__name__}: {exc!s}",
            ),
        )
    finally:
        stdin.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await stdin.wait_closed()


def _spawn_stdin_writer(
    process: asyncio.subprocess.Process,
    stdin_data: bytes | None,
    observation: _StageObservation,
) -> asyncio.Task[None] | None:
    """Start stdin writing when direct stdin data was supplied."""
    if stdin_data is None:
        return None
    return asyncio.create_task(_write_stdin(process, stdin_data, observation))


def _create_stream_callback(
    observation: _StageObservation,
    event_type: typ.Literal["stdout", "stderr"],
    pid: int | None,
) -> cabc.Callable[[str], None] | None:
    """Create a callback for emitting stream line events, or None if no hooks."""
    if not observation.hooks.observe_hooks:
        return None
    return lambda line: observation.emit(event_type, _EventDetails(pid=pid, line=line))


def _spawn_stream_consumers(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    stream_config: _StreamConfig,
    *,
    pid: int | None,
) -> tuple[asyncio.Task[str | None], asyncio.Task[str | None]]:
    """Spawn stdout and stderr stream consumer tasks."""
    stdout_on_line = _create_stream_callback(execution.observation, "stdout", pid)
    stderr_on_line = _create_stream_callback(execution.observation, "stderr", pid)
    stderr_config = dc.replace(
        stream_config,
        sink=(
            execution.ctx.stderr_sink
            if execution.ctx.stderr_sink is not None
            else sys.stderr
        ),
    )
    return (
        asyncio.create_task(
            _consume_stream(
                process.stdout,
                stream_config,
                on_line=stdout_on_line,
            ),
        ),
        asyncio.create_task(
            _consume_stream(
                process.stderr,
                stderr_config,
                on_line=stderr_on_line,
            ),
        ),
    )


def _build_stream_config(execution: _SubprocessExecution) -> _StreamConfig:
    """Build the stdout _StreamConfig for an execution context."""
    return _StreamConfig(
        capture_output=execution.capture,
        echo_output=execution.echo,
        sink=(
            execution.ctx.stdout_sink
            if execution.ctx.stdout_sink is not None
            else sys.stdout
        ),
        encoding=execution.ctx.encoding,
        errors=execution.ctx.errors,
    )


async def _handle_stream_timeout(
    exc: TimeoutError,
    *,
    stdin_task: asyncio.Task[None] | None,
    consumers: tuple[asyncio.Task[str | None], asyncio.Task[str | None]],
    timeout: float | None,
) -> typ.NoReturn:
    """Clean up stdin/stream tasks on timeout and raise _SubprocessTimeoutError."""
    if stdin_task is not None:
        await asyncio.gather(stdin_task, return_exceptions=True)
    stdout_text, stderr_text = await asyncio.gather(*consumers)
    if timeout is None:
        msg = "TimeoutError without a configured timeout"
        raise RuntimeError(msg) from exc
    raise _SubprocessTimeoutError(
        _SubprocessTimeoutDetails(
            timeout=timeout,
            stdout=stdout_text,
            stderr=stderr_text,
            exited_at=time.perf_counter(),
        ),
    ) from exc


async def _run_subprocess_with_streams(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    *,
    pid: int | None,
    timeout: float | None,
) -> tuple[int, float, str | None, str | None]:
    """Run subprocess with stream capture and timeout handling."""
    stream_config = _build_stream_config(execution)
    consumers = _spawn_stream_consumers(process, execution, stream_config, pid=pid)
    stdin_task = _spawn_stdin_writer(
        process, execution.stdin_data, execution.observation
    )
    try:
        exit_code, exited_at = await _wait_for_exit_code(
            process,
            execution.ctx,
            timeout=timeout,
            consumers=consumers,
        )
    except TimeoutError as exc:
        await _handle_stream_timeout(
            exc, stdin_task=stdin_task, consumers=consumers, timeout=timeout
        )
    except asyncio.CancelledError:
        if stdin_task is not None:
            stdin_task.cancel()
            await asyncio.gather(stdin_task, return_exceptions=True)
        raise
    if stdin_task is not None:
        await stdin_task
    stdout_text, stderr_text = await asyncio.gather(*consumers)
    return exit_code, exited_at, stdout_text, stderr_text


def _get_exit_code(process: asyncio.subprocess.Process) -> int:
    """Return the process exit code, defaulting to -1 if unavailable."""
    return process.returncode if process.returncode is not None else -1


@dc.dataclass(frozen=True, slots=True)
class _ExitEventDetails:
    """Parameters for emitting an exit event."""

    pid: int | None
    exit_code: int
    started_at: float
    exited_at: float


def _emit_exit_event(
    observation: _StageObservation,
    details: _ExitEventDetails,
) -> None:
    """Emit an exit event with process details and duration."""
    observation.emit(
        "exit",
        _EventDetails(
            pid=details.pid,
            exit_code=details.exit_code,
            duration_s=max(0.0, details.exited_at - details.started_at),
        ),
    )


@dc.dataclass(frozen=True, slots=True)
class _TimeoutContext:
    """Context information for a timeout exception."""

    cmd_argv: tuple[str, ...]
    timeout: float
    stdout: str | None
    stderr: str | None


def _raise_timeout_expired(
    timeout_ctx: _TimeoutContext,
    exc: BaseException,
) -> typ.NoReturn:
    """Raise TimeoutExpired with captured output and chain the original exception."""
    raise _sh_module().TimeoutExpired(
        cmd=timeout_ctx.cmd_argv,
        timeout=timeout_ctx.timeout,
        output=timeout_ctx.stdout,
        stderr=timeout_ctx.stderr,
    ) from exc


@dc.dataclass(frozen=True, slots=True)
class _SubprocessTimeoutContext:
    """Context for handling subprocess timeout exceptions."""

    execution: _SubprocessExecution
    process: asyncio.subprocess.Process
    started_at: float
    stdout_text: str | None
    stderr_text: str | None


def _handle_subprocess_timeout(
    ctx: _SubprocessTimeoutContext,
    exc: TimeoutError | _SubprocessTimeoutError,
) -> typ.NoReturn:
    """Handle a subprocess timeout by emitting exit event and raising TimeoutExpired."""
    if isinstance(exc, _SubprocessTimeoutError):
        timeout = exc.timeout
        exited_at = exc.exited_at
        stdout = exc.stdout
        stderr = exc.stderr
    else:
        timeout = ctx.execution.timeout
        if timeout is None:
            msg = "TimeoutError without a configured timeout"
            raise RuntimeError(msg) from exc
        exited_at = time.perf_counter()
        stdout = ctx.stdout_text
        stderr = ctx.stderr_text

    exit_code = _get_exit_code(ctx.process)
    _emit_exit_event(
        ctx.execution.observation,
        _ExitEventDetails(
            pid=ctx.process.pid,
            exit_code=exit_code,
            started_at=ctx.started_at,
            exited_at=exited_at,
        ),
    )
    _raise_timeout_expired(
        _TimeoutContext(
            cmd_argv=ctx.execution.cmd.argv_with_program,
            timeout=timeout,
            stdout=stdout,
            stderr=stderr,
        ),
        exc,
    )


async def _execute_subprocess(execution: _SubprocessExecution) -> CommandResult:
    """Execute a subprocess and return the command result."""
    process = await _spawn_subprocess(execution)
    started_at = time.perf_counter()
    pid = process.pid
    execution.observation.emit("start", _EventDetails(pid=pid))

    stdout_text: str | None = None
    stderr_text: str | None = None
    try:
        if not execution.capture and not execution.echo:
            stdin_task = _spawn_stdin_writer(
                process, execution.stdin_data, execution.observation
            )
            cleanup_tasks = (stdin_task,) if stdin_task is not None else ()
            exit_code, exited_at = await _wait_for_exit_code(
                process,
                execution.ctx,
                timeout=execution.timeout,
                consumers=cleanup_tasks,
            )
            if stdin_task is not None:
                await stdin_task
        else:
            (
                exit_code,
                exited_at,
                stdout_text,
                stderr_text,
            ) = await _run_subprocess_with_streams(
                process,
                execution,
                pid=pid,
                timeout=execution.timeout,
            )
    except (TimeoutError, _SubprocessTimeoutError) as exc:
        _handle_subprocess_timeout(
            _SubprocessTimeoutContext(
                execution=execution,
                process=process,
                started_at=started_at,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
            ),
            exc,
        )

    _emit_exit_event(
        execution.observation,
        _ExitEventDetails(
            pid=pid,
            exit_code=exit_code,
            started_at=started_at,
            exited_at=exited_at,
        ),
    )

    return _sh_module().CommandResult(
        program=execution.cmd.program,
        argv=execution.cmd.argv,
        exit_code=exit_code,
        pid=process.pid if process.pid is not None else -1,
        stdout=stdout_text,
        stderr=stderr_text,
    )


__all__ = [
    "_ExitEventDetails",
    "_SubprocessExecution",
    "_SubprocessTimeoutContext",
    "_SubprocessTimeoutDetails",
    "_SubprocessTimeoutError",
    "_TimeoutContext",
    "_build_stream_config",
    "_create_stream_callback",
    "_emit_exit_event",
    "_execute_subprocess",
    "_get_exit_code",
    "_handle_stream_timeout",
    "_handle_subprocess_timeout",
    "_raise_timeout_expired",
    "_resolve_timeout",
    "_run_subprocess_with_streams",
    "_spawn_stream_consumers",
    "_spawn_subprocess",
    "_wait_for_exit_code",
]
