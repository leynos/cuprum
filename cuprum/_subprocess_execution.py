"""Internal subprocess execution machinery.

This module encapsulates the low-level subprocess spawning, stream handling,
timeout management, and execution lifecycle for SafeCmd.run().
"""
# TODO: refactor into smaller submodules (stdin/runner), see issue #30.
# pylint: disable=too-many-lines

from __future__ import annotations

import asyncio
import dataclasses as dc
import sys
import time
import typing as typ

from cuprum._pipeline_types import _EventDetails, _StageObservation
from cuprum._process_lifecycle import _merge_env, _terminate_process
from cuprum._streams import _consume_stream, _StreamConfig
from cuprum._subprocess_context import _cwd_arg, _sh_module
from cuprum._subprocess_stdin import _cancel_stdin_writer, _spawn_stdin_writer
from cuprum._subprocess_timeout import (
    _emit_exit_event,
    _ExitEventDetails,
    _handle_stream_timeout,
    _handle_subprocess_timeout,
    _SubprocessTimeoutContext,
    _SubprocessTimeoutError,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import CommandResult, ExecutionContext, SafeCmd


def _cancel_pending_consumers(
    consumers: tuple[asyncio.Task[str | None], ...],
) -> None:
    """Cancel each consumer task that has not already completed.

    Finished readers keep their captured output; only tasks still blocked
    after process termination (or on cancellation) are cancelled, so cleanup
    cannot hang on a reader wedged on a pipe that never reached EOF.
    """
    for task in consumers:
        if not task.done():
            task.cancel()


async def _wait_for_exit_code(
    process: asyncio.subprocess.Process,
    ctx: ExecutionContext,
    *,
    timeout: float | None = None,
) -> tuple[int, float]:
    """Wait for a subprocess exit code, terminating it on timeout or cancel.

    Waiting for the exit code is this helper's sole responsibility. Any stream
    consumers belong to the caller, which drains them exactly once when the wait
    fails (see :func:`_run_subprocess_with_streams`); terminating the process
    here lets those consumers reach EOF during that drain.
    """
    try:
        if timeout is None:
            exit_code = await process.wait()
        else:
            exit_code = await asyncio.wait_for(process.wait(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        # asyncio.wait_for raises TimeoutError on expiry; the surrounding task
        # may also be cancelled. Both tear the process down before re-raising
        # the original exception.
        await _terminate_process(process, ctx.cancel_grace)
        raise
    exited_at = time.perf_counter()
    return exit_code, exited_at


async def _drain_stream_consumers(
    consumers: tuple[asyncio.Task[str | None], asyncio.Task[str | None]],
) -> tuple[str | None, str | None]:
    """Cancel pending consumers, drain them once, and decode their output.

    A consumer that failed or was cancelled maps to ``None`` so a broken reader
    cannot mask the surrounding failure. Draining here exactly once keeps the
    timeout and cancellation paths from reconciling the same tasks twice.
    """
    _cancel_pending_consumers(consumers)
    stdout_result, stderr_result = await asyncio.gather(
        *consumers, return_exceptions=True
    )
    stdout_text = None if isinstance(stdout_result, BaseException) else stdout_result
    stderr_text = None if isinstance(stderr_result, BaseException) else stderr_result
    return stdout_text, stderr_text


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
        cwd=_cwd_arg(execution.ctx.cwd),
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
        )
    except TimeoutError as exc:
        # The process has been terminated; cancel the stdin writer and drain the
        # stream consumers exactly once here, then hand the decoded output to the
        # timeout handler so it survives on the resulting TimeoutExpired.
        await _cancel_stdin_writer(stdin_task)
        stdout_text, stderr_text = await _drain_stream_consumers(consumers)
        _handle_stream_timeout(
            exc,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            timeout=timeout,
        )
    except asyncio.CancelledError:
        await _cancel_stdin_writer(stdin_task)
        await _drain_stream_consumers(consumers)
        raise
    if stdin_task is not None:
        try:
            await stdin_task
        except BaseException:
            # An unexpected stdin-writer failure (or a cancellation landing on
            # this await) must still reconcile the stdout/stderr consumers,
            # mirroring the timeout and cancellation paths above, so those tasks
            # are cancelled and drained before the error propagates.
            await _drain_stream_consumers(consumers)
            raise
    stdout_text, stderr_text = await asyncio.gather(*consumers)
    return exit_code, exited_at, stdout_text, stderr_text


async def _run_subprocess_without_streams(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
) -> tuple[int, float]:
    """Run a subprocess without stream capture or echo."""
    stdin_task = _spawn_stdin_writer(
        process, execution.stdin_data, execution.observation
    )
    try:
        exit_code, exited_at = await _wait_for_exit_code(
            process,
            execution.ctx,
            timeout=execution.timeout,
        )
    except (TimeoutError, asyncio.CancelledError):
        # Manage the stdin writer separately from _wait_for_exit_code's
        # consumers: cancel and drain it before the timeout is
        # translated or the cancellation propagates, so a stdin drain
        # blocked on an unread pipe cannot delay completion.
        await _cancel_stdin_writer(stdin_task)
        raise
    if stdin_task is not None:
        await stdin_task
    return exit_code, exited_at


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
            exit_code, exited_at = await _run_subprocess_without_streams(
                process, execution
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
    "_SubprocessExecution",
    "_build_stream_config",
    "_cancel_pending_consumers",
    "_create_stream_callback",
    "_drain_stream_consumers",
    "_execute_subprocess",
    "_run_subprocess_with_streams",
    "_run_subprocess_without_streams",
    "_spawn_stream_consumers",
    "_spawn_subprocess",
    "_wait_for_exit_code",
]
