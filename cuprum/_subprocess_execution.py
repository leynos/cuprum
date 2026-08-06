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
) -> tuple[int, float]:
    """Wait for a subprocess exit code, terminating it on expiry or cancel.

    Waiting for the exit code is this helper's sole responsibility. Any stream
    consumers belong to the caller, which drains them exactly once when the wait
    fails (see :func:`_run_subprocess_with_streams`); terminating the process
    here lets those consumers reach EOF during that drain.

    Callers also own the deadline: wrap the call in ``async with
    asyncio.timeout(...)`` to bound the wait. A deadline expiry cancels this
    task, so it arrives here as :class:`asyncio.CancelledError` and is torn
    down identically to an externally requested cancellation. The enclosing
    ``asyncio.timeout`` block re-raises expiry as :class:`TimeoutError` once the
    teardown re-raises, while a genuine external cancellation propagates
    unchanged.
    """
    try:
        exit_code = await process.wait()
    except asyncio.CancelledError:
        # A deadline expiry (via asyncio.timeout) and an external cancellation
        # both surface here as CancelledError and need the same teardown:
        # terminate the process so the caller's drain can reach EOF, then
        # re-raise so the cancellation can propagate.
        await _terminate_process(process, ctx.cancel_grace)
        raise
    exited_at = time.perf_counter()
    return exit_code, exited_at


# How long a capturing drain lets readers observe EOF before cancelling them.
# The process is already dead by then, so EOF is imminent rather than
# hypothetical, and a reader cancelled a scheduling turn short of it loses the
# capture it was about to deliver. The window stays short because a grandchild
# holding the pipe can wedge a reader indefinitely.
_CAPTURE_EOF_GRACE_S = 0.25


def _decode_consumer_result(
    result: str | BaseException | None,
    *,
    capture: bool,
) -> str | None:
    """Decode one drained consumer into the text its stream reports.

    A reader that failed, was cancelled, or was never capturing has no text of
    its own. A capturing run promised a string, so it reports the empty string;
    a non-capturing run reports ``None``, having never had text to report.
    """
    if isinstance(result, BaseException) or result is None:
        return "" if capture else None
    return result


async def _drain_stream_consumers(
    consumers: tuple[asyncio.Task[str | None], asyncio.Task[str | None]],
    *,
    capture: bool,
) -> tuple[str | None, str | None]:
    """Settle pending consumers, drain them once, and decode their output.

    Draining exactly once keeps the timeout and cancellation paths from
    reconciling the same tasks twice. Under ``capture`` the readers first get a
    bounded window to reach EOF (see :data:`_CAPTURE_EOF_GRACE_S`), and one that
    still has no text reports the empty string. Pass ``capture=False`` where the
    drained text is discarded, so teardown pays neither window nor contract.
    """
    if capture:
        await asyncio.wait(consumers, timeout=_CAPTURE_EOF_GRACE_S)
    _cancel_pending_consumers(consumers)
    stdout_result, stderr_result = await asyncio.gather(
        *consumers, return_exceptions=True
    )
    return (
        _decode_consumer_result(stdout_result, capture=capture),
        _decode_consumer_result(stderr_result, capture=capture),
    )


async def _wait_for_exit_code_within_timeout(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
) -> tuple[int, float]:
    """Await the exit code under ``execution.timeout``, or unbounded when ``None``.

    A non-positive timeout denotes an already-elapsed deadline. ``asyncio.timeout``
    would only schedule its cancellation for the next event-loop iteration, so a
    fast, already-exited process whose ``wait()`` never suspends would race past
    it and return successfully. To keep ``run(timeout=0)`` deterministic — and to
    preserve the behaviour of the ``asyncio.wait_for`` implementation this
    replaced — a non-positive deadline expires immediately: the deadline wait
    is skipped, so the process is never given the chance to exit on its own,
    but terminating it still awaits its actual exit before
    :class:`TimeoutError` is raised.

    Stream consumers belong to the caller, which drains them exactly once via
    :func:`_drain_stream_consumers`; terminating the process here lets those
    consumers reach EOF during that drain.
    """
    timeout = execution.timeout
    if timeout is not None and timeout <= 0:
        await _terminate_process(process, execution.ctx.cancel_grace)
        raise TimeoutError
    async with asyncio.timeout(timeout):
        return await _wait_for_exit_code(process, execution.ctx)


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
) -> tuple[int, float, str | None, str | None]:
    """Run subprocess with stream capture and timeout handling."""
    stream_config = _build_stream_config(execution)
    consumers = _spawn_stream_consumers(process, execution, stream_config, pid=pid)
    stdin_task = _spawn_stdin_writer(
        process, execution.stdin_data, execution.observation
    )
    try:
        exit_code, exited_at = await _wait_for_exit_code_within_timeout(
            process,
            execution,
        )
    except TimeoutError as exc:
        # The process has been terminated; cancel the stdin writer and drain the
        # stream consumers exactly once here, then hand the decoded output to the
        # timeout handler so it survives on the resulting TimeoutExpired.
        await _cancel_stdin_writer(stdin_task)
        stdout_text, stderr_text = await _drain_stream_consumers(
            consumers,
            capture=execution.capture,
        )
        _handle_stream_timeout(
            exc,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            timeout=execution.timeout,
        )
    except asyncio.CancelledError:
        await _cancel_stdin_writer(stdin_task)
        # The drained text is discarded here: a cancelled run raises rather than
        # reporting output, so the capture contract does not apply and teardown
        # need not wait for readers to reach EOF.
        await _drain_stream_consumers(consumers, capture=False)
        raise
    if stdin_task is not None:
        try:
            await stdin_task
        except BaseException:
            # An unexpected stdin-writer failure (or a cancellation landing on
            # this await) must still reconcile the stdout/stderr consumers,
            # mirroring the timeout and cancellation paths above, so those tasks
            # are cancelled and drained before the error propagates. Its text is
            # discarded, so this drain skips the capture contract too.
            await _drain_stream_consumers(consumers, capture=False)
            raise
    stdout_text, stderr_text = await asyncio.gather(*consumers)
    return exit_code, exited_at, stdout_text, stderr_text


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
            try:
                exit_code, exited_at = await _wait_for_exit_code_within_timeout(
                    process,
                    execution,
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
    "_decode_consumer_result",
    "_drain_stream_consumers",
    "_execute_subprocess",
    "_run_subprocess_with_streams",
    "_spawn_stream_consumers",
    "_spawn_subprocess",
    "_wait_for_exit_code",
    "_wait_for_exit_code_within_timeout",
]
