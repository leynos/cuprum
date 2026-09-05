"""Internal subprocess execution machinery.

Orchestration for ``SafeCmd.run()``: spawning the subprocess, wiring its
stream consumers, and assembling the ``CommandResult``. The rules for ending a
run — applying the deadline, terminating the process, and draining the stream
consumers exactly once — live in ``cuprum._subprocess_wait``.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import sys
import time
import typing as typ

from cuprum._pipeline_types import _EventDetails, _StageObservation
from cuprum._process_lifecycle import _merge_env, _shielded_cleanup
from cuprum._streams import _consume_stream, _RelayDiagnostics, _StreamConfig
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
from cuprum._subprocess_wait import (
    _drain_stream_consumers,
    _DrainContext,
    _reconcile_run_tasks,
    _RunTaskOwnership,
    _wait_for_exit_code_within_timeout,
)
from cuprum.echo_events import EchoStream

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import CommandResult, ExecutionContext, SafeCmd


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
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics],
) -> tuple[asyncio.Task[str | None], asyncio.Task[str | None]]:
    """Spawn stdout and stderr stream consumer tasks.

    Each consumer drains into its collector from ``relay_diagnostics``:
    index ``0`` is stdout's, index ``1`` is stderr's. The caller retains the
    pair on its ``_RunTaskOwnership`` so its single reconciliation point can
    settle and read them exactly once.
    """
    stdout_on_line = _create_stream_callback(execution.observation, "stdout", pid)
    stderr_on_line = _create_stream_callback(execution.observation, "stderr", pid)
    stderr_config = dc.replace(
        stream_config,
        sink=(
            execution.ctx.stderr_sink
            if execution.ctx.stderr_sink is not None
            else sys.stderr
        ),
        stream=EchoStream.STDERR,
    )
    return (
        asyncio.create_task(
            _consume_stream(
                process.stdout,
                stream_config,
                on_line=stdout_on_line,
                relay_diagnostics=relay_diagnostics[0],
            ),
        ),
        asyncio.create_task(
            _consume_stream(
                process.stderr,
                stderr_config,
                on_line=stderr_on_line,
                relay_diagnostics=relay_diagnostics[1],
            ),
        ),
    )


def _build_stream_config(
    execution: _SubprocessExecution,
    discard_on_cancel: asyncio.Event,
) -> _StreamConfig:
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
        discard_on_cancel=discard_on_cancel,
    )


async def _wait_for_streamed_process_exit(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    tasks: _RunTaskOwnership,
    pid: int | None,
) -> tuple[int, float]:
    """Wait for exit and reconcile every stream task when that wait fails."""
    try:
        return await _wait_for_exit_code_within_timeout(
            process,
            execution,
        )
    except TimeoutError as exc:
        # The process has been terminated; cancel the stdin writer and drain the
        # stream consumers exactly once here, then hand the decoded output to the
        # timeout handler so it survives on the resulting TimeoutExpired. The
        # reconciliation is shielded because a caller cancelling now would
        # otherwise abandon the consumers mid-drain and leak them.
        stdout_text, stderr_text = await _shielded_cleanup(
            _reconcile_run_tasks(
                tasks,
                _DrainContext(
                    capture=execution.capture,
                    pid=pid,
                    observation=execution.observation,
                    discard_on_cancel=tasks.discard_on_cancel,
                ),
            )
        )
        _handle_stream_timeout(
            exc,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            timeout=execution.timeout,
        )
    except BaseException:
        # Cancellation, and any other failure escaping the wait — an OS error
        # while terminating, say — need the same reconciliation. Do not capture
        # stream text while another error propagates, but settle every task
        # before re-raising the original failure unchanged.
        await _shielded_cleanup(
            _reconcile_run_tasks(
                tasks,
                _DrainContext(
                    capture=False,
                    pid=pid,
                    observation=execution.observation,
                    discard_on_cancel=tasks.discard_on_cancel,
                ),
            )
        )
        raise


async def _run_subprocess_with_streams(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    *,
    pid: int | None,
) -> tuple[
    int,
    float,
    str | None,
    str | None,
    tuple[_RelayDiagnostics, _RelayDiagnostics],
]:
    """Run subprocess with stream capture, timeout handling, and diagnostics.

    Returns
    -------
    tuple[int, float, str | None, str | None, tuple[_RelayDiagnostics, _RelayDiagnostics]]
        The exit code, exit timestamp, captured stdout, captured stderr, and
        the per-stream relay diagnostics collectors (stdout first, stderr
        second) settled by the run's reconciliation.
    """
    discard_on_cancel = asyncio.Event()
    stream_config = _build_stream_config(execution, discard_on_cancel)
    relay_diagnostics = (_RelayDiagnostics(), _RelayDiagnostics())
    tasks = _RunTaskOwnership(
        stdin_task=_spawn_stdin_writer(
            process, execution.stdin_data, execution.observation
        ),
        consumers=_spawn_stream_consumers(
            process,
            execution,
            stream_config,
            pid=pid,
            relay_diagnostics=relay_diagnostics,
        ),
        discard_on_cancel=discard_on_cancel,
        relay_diagnostics=relay_diagnostics,
    )
    exit_code, exited_at = await _wait_for_streamed_process_exit(
        process,
        execution,
        tasks,
        pid,
    )
    if tasks.stdin_task is not None:
        try:
            await tasks.stdin_task
        except BaseException:
            # An unexpected stdin-writer failure (or a cancellation landing on
            # this await) must still reconcile the stdout/stderr consumers,
            # mirroring the timeout and cancellation paths above, so those tasks
            # are cancelled and drained before the error propagates. The writer
            # has already settled here, so only the consumers need draining.
            await _shielded_cleanup(
                _drain_stream_consumers(
                    tasks.consumers,
                    _DrainContext(
                        capture=False,
                        pid=pid,
                        observation=execution.observation,
                        discard_on_cancel=tasks.discard_on_cancel,
                    ),
                )
            )
            raise
    try:
        stdout_text, stderr_text = await asyncio.gather(*tasks.consumers)
        for diagnostics in tasks.relay_diagnostics:
            diagnostics.settle()
    except BaseException:
        # `gather` re-raises the first failure and leaves its sibling running,
        # so a reader wedged on a pipe would outlive the run it belonged to.
        # Reconcile it the way every other exit path does, then re-raise: the
        # drain absorbs what it finds, which is right while another error is
        # propagating — and here the consumer failure *is* that error.
        await _shielded_cleanup(
            _drain_stream_consumers(
                tasks.consumers,
                _DrainContext(
                    capture=False,
                    pid=pid,
                    observation=execution.observation,
                    discard_on_cancel=tasks.discard_on_cancel,
                ),
            )
        )
        raise
    return exit_code, exited_at, stdout_text, stderr_text, relay_diagnostics


async def _run_subprocess_without_streams(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
) -> tuple[int, float]:
    """Run a subprocess directly, without stdout/stderr capture or echo.

    The direct path spawns no stream consumers, so the only task to reconcile is
    the stdin writer. Whatever escapes the wait — a timeout, a cancellation, or
    an unexpected failure — it is cancelled and drained through
    :func:`_cancel_stdin_writer` *before* the exception propagates, so a stdin
    drain blocked on an unread pipe cannot delay timeout translation or
    cancellation, and no writer is left running behind a failure. That cleanup
    is shielded, so a cancellation arriving while it runs cannot abandon it. An
    unexpected stdin-writer failure after the process exits normally propagates
    unchanged.

    Returns
    -------
    tuple[int, float]
        The process exit code and the ``perf_counter`` timestamp of exit.
    """
    stdin_task = _spawn_stdin_writer(
        process, execution.stdin_data, execution.observation
    )
    try:
        exit_code, exited_at = await _wait_for_exit_code_within_timeout(
            process,
            execution,
        )
    except BaseException:
        await _shielded_cleanup(_cancel_stdin_writer(stdin_task))
        raise
    if stdin_task is not None:
        await stdin_task
    return exit_code, exited_at


def _relay_fallbacks_for_result(
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics] | None,
) -> tuple[RelayFallback, ...]:
    """Flatten per-stream diagnostics into one result tuple.

    The order is stdout's records then stderr's; each record carries its own
    stream, and this order does not reconstruct chronological interleaving
    between the two streams.
    """
    if relay_diagnostics is None:
        return ()
    return relay_diagnostics[0].snapshot() + relay_diagnostics[1].snapshot()


async def _execute_subprocess(execution: _SubprocessExecution) -> CommandResult:
    """Execute a subprocess and return the command result."""
    process = await _spawn_subprocess(execution)
    started_at = time.perf_counter()
    pid = process.pid
    execution.observation.emit("start", _EventDetails(pid=pid))

    # Left as None by the direct path, which captures nothing; the stream path
    # overwrites them with whatever it captured before returning.
    stdout_text: str | None = None
    stderr_text: str | None = None
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics] | None = None
    try:
        if execution.capture or execution.echo:
            (
                exit_code,
                exited_at,
                stdout_text,
                stderr_text,
                relay_diagnostics,
            ) = await _run_subprocess_with_streams(
                process,
                execution,
                pid=pid,
            )
        else:
            exit_code, exited_at = await _run_subprocess_without_streams(
                process,
                execution,
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
        relay_fallbacks=_relay_fallbacks_for_result(relay_diagnostics),
    )


__all__ = [
    "_SubprocessExecution",
    "_build_stream_config",
    "_create_stream_callback",
    "_execute_subprocess",
    "_run_subprocess_with_streams",
    "_run_subprocess_without_streams",
    "_spawn_stream_consumers",
    "_spawn_subprocess",
]
