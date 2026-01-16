"""Internal subprocess execution machinery.

This module encapsulates the low-level subprocess spawning, stream handling,
timeout management, and execution lifecycle for SafeCmd.run().
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import sys
import time
import typing as typ

from cuprum._pipeline_internals import _EventDetails, _StageObservation
from cuprum._process_lifecycle import _merge_env, _terminate_process
from cuprum._streams import _consume_stream, _StreamConfig

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.context import CuprumContext
    from cuprum.sh import CommandResult, ExecutionContext, SafeCmd


def _sh_module() -> typ.Any:  # noqa: ANN401 — returns module, typed access via attributes
    """Lazy import sh module to avoid circular imports."""
    from cuprum import sh

    return sh


def _current_context() -> CuprumContext:
    """Get the current context via lazy import to avoid circular imports."""
    from cuprum.context import current_context

    return current_context()


def _resolve_timeout(
    *,
    timeout: float | None,
    context: ExecutionContext | None,
) -> float | None:
    """Resolve the effective timeout from explicit, context, and scoped values."""
    if timeout is not None:
        return timeout
    if context is not None and context.timeout is not None:
        return context.timeout
    return _current_context().timeout


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
    else:
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


class _SubprocessTimeoutError(Exception):
    """Internal wrapper for subprocess timeouts with captured output."""

    def __init__(
        self,
        *,
        timeout: float,
        stdout: str | None,
        stderr: str | None,
        exited_at: float,
    ) -> None:
        super().__init__(f"Execution exceeded {timeout}s timeout")
        self.timeout = timeout
        self.stdout = stdout
        self.stderr = stderr
        self.exited_at = exited_at


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
        env=_merge_env(execution.ctx.env),
        cwd=(str(execution.ctx.cwd) if execution.ctx.cwd is not None else None),
    )


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


async def _run_subprocess_with_streams(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    *,
    pid: int | None,
    timeout: float | None,
) -> tuple[int, float, str | None, str | None]:
    """Run subprocess with stream capture and timeout handling."""
    stream_config = _StreamConfig(
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
    consumers = _spawn_stream_consumers(process, execution, stream_config, pid=pid)
    try:
        exit_code, exited_at = await _wait_for_exit_code(
            process,
            execution.ctx,
            timeout=timeout,
            consumers=consumers,
        )
    except TimeoutError as exc:
        stdout_text, stderr_text = await asyncio.gather(*consumers)
        # Invariant: TimeoutError only raised when timeout is configured
        if timeout is None:
            msg = "TimeoutError without a configured timeout"
            raise RuntimeError(msg) from exc
        raise _SubprocessTimeoutError(
            timeout=timeout,
            stdout=stdout_text,
            stderr=stderr_text,
            exited_at=time.perf_counter(),
        ) from exc
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
            exit_code, exited_at = await _wait_for_exit_code(
                process,
                execution.ctx,
                timeout=execution.timeout,
            )
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
    "_SubprocessTimeoutError",
    "_TimeoutContext",
    "_create_stream_callback",
    "_emit_exit_event",
    "_execute_subprocess",
    "_get_exit_code",
    "_handle_subprocess_timeout",
    "_raise_timeout_expired",
    "_resolve_timeout",
    "_run_subprocess_with_streams",
    "_spawn_stream_consumers",
    "_spawn_subprocess",
    "_wait_for_exit_code",
]
