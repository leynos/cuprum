"""Internal subprocess execution machinery.

Orchestration for ``SafeCmd.run()``: spawning the subprocess, wiring its
stream consumers, and assembling the ``CommandResult``. The rules for ending a
run — applying the deadline, terminating the process, and draining the stream
consumers exactly once — live in ``cuprum._subprocess_wait``. The streamed
run loop that waits for exit and reconciles the consumer tasks lives in
``cuprum._subprocess_stream_run``, and the consumer construction those two
drive lives in ``cuprum._subprocess_streams``; both are re-exported here so
importers of this module keep working unchanged.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import time
import typing as typ

from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._pipeline_types import _EventDetails, _StageObservation
from cuprum._process_lifecycle import _merge_env, _shielded_cleanup
from cuprum._rusage import capture_child_rusage, child_rusage_delta
from cuprum._subprocess_context import _cwd_arg, _sh_module
from cuprum._subprocess_stdin import _cancel_stdin_writer, _spawn_stdin_writer
from cuprum._subprocess_stream_run import _run_subprocess_with_streams
from cuprum._subprocess_streams import (
    _build_stream_config,
    _create_stream_callback,
    _spawn_stream_consumers,
    _StreamConsumerSpawnContext,
)
from cuprum._subprocess_timeout import (
    _emit_exit_event,
    _ExitEventDetails,
    _handle_subprocess_timeout,
    _SubprocessTimeoutContext,
    _SubprocessTimeoutError,
)
from cuprum._subprocess_wait import _wait_for_exit_code_within_timeout

if typ.TYPE_CHECKING:
    from cuprum._idle_heartbeat import _IdleMonitor
    from cuprum._streams import _RelayDiagnostics
    from cuprum.echo_events import RelayFallback
    from cuprum.lines import _LineHookFn
    from cuprum.sh import CommandResult, ExecutionContext, SafeCmd
    from cuprum.sinks.base import OutputSession


@dc.dataclass(frozen=True, slots=True)
class _SubprocessExecution:
    """Execution context bundle for subprocess spawning."""

    cmd: SafeCmd

    ctx: ExecutionContext

    capture: bool

    echo_stdout: bool

    echo_stderr: bool

    max_echo_line_bytes: int | None

    sink_session: OutputSession | None

    timeout: float | None

    observation: _StageObservation

    stdin_data: bytes | None
    on_line: _LineHookFn | None = None
    # Monotonic reference the per-line ``at`` stamps are measured from; taken
    # once at spawn so every line of a run shares one time base.
    started_at: float = 0.0
    idle: _IdleMonitor | None = None

    @property
    def consumes_stdout(self) -> bool:
        """Whether the parent must consume stdout, rather than discard it.

        A registered ``on_line`` observes both streams, so it keeps the pipe
        open and the consumer running even when capture and echo are both off.
        Without that, ``run(output=RunOutputOptions(on_line=...))`` would attach
        stdout to ``DEVNULL`` and silently deliver nothing.
        """
        return (
            self.capture
            or self.echo_stdout
            or self.idle is not None
            or self.on_line is not None
        )

    @property
    def consumes_stderr(self) -> bool:
        """Whether the parent must consume stderr, rather than discard it."""
        return (
            self.capture
            or self.echo_stderr
            or self.idle is not None
            or self.on_line is not None
        )


async def _spawn_subprocess(
    execution: _SubprocessExecution,
) -> asyncio.subprocess.Process:
    """Spawn an async subprocess with configured I/O and environment."""
    return await asyncio.create_subprocess_exec(
        *execution.cmd.argv_with_program,
        stdout=(
            asyncio.subprocess.PIPE
            if execution.consumes_stdout
            else asyncio.subprocess.DEVNULL
        ),
        stderr=(
            asyncio.subprocess.PIPE
            if execution.consumes_stderr
            else asyncio.subprocess.DEVNULL
        ),
        stdin=(asyncio.subprocess.PIPE if execution.stdin_data is not None else None),
        env=_merge_env(execution.ctx.env),
        cwd=_cwd_arg(execution.ctx.cwd),
    )


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

    Returns
    -------
    tuple[RelayFallback, ...]
        The command's handled echo-disablement records, empty when its
        diagnostics are absent or recorded nothing.
    """
    if relay_diagnostics is None:
        return ()
    return relay_diagnostics[0].snapshot() + relay_diagnostics[1].snapshot()


async def _execute_subprocess(execution: _SubprocessExecution) -> CommandResult:
    """Execute a subprocess and return the command result."""
    rusage_before = capture_child_rusage()
    started_at = time.perf_counter()
    # The published result timestamp is a separate reading from the monotonic
    # one above: ``started_at`` is a monotonic reference the line stamps and
    # the duration are measured against, while this is the wall-clock instant
    # the result reports to callers. Both are sampled before the spawn await,
    # so a run's recorded duration includes the time the spawn itself blocked.
    wall_clock_started_at = execution.observation.wall_clock()
    # Rebuilt, not mutated: the bundle is a frozen dataclass, and the stream
    # consumers read ``started_at`` off it when stamping each ``LineEvent``.
    # Left at its ``0.0`` default, every ``at`` would be the machine's monotonic
    # uptime rather than seconds since this command started.
    execution = dc.replace(execution, started_at=started_at)
    process = await _spawn_subprocess(execution)
    pid = process.pid
    execution.observation.emit("start", _EventDetails(pid=pid))
    # The direct path captures nothing; the stream path overwrites these values.
    stdout_text: str | None = None
    stderr_text: str | None = None
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics] | None = None
    try:
        if execution.consumes_stdout or execution.consumes_stderr:
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
    finally:
        # Every exit path settles the watchdog exactly once, including the
        # failures converted above and any that bypass the stream helpers
        # entirely; repeats are no-ops.
        await _shielded_cleanup(_stop_idle_monitor(execution.idle))

    rusage = child_rusage_delta(rusage_before, capture_child_rusage())
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
        started_at=wall_clock_started_at,
        duration=max(0.0, exited_at - started_at),
        max_rss_bytes=None if rusage is None else rusage.max_rss_bytes,
        user_cpu_seconds=None if rusage is None else rusage.user_cpu_seconds,
        system_cpu_seconds=None if rusage is None else rusage.system_cpu_seconds,
        relay_fallbacks=_relay_fallbacks_for_result(relay_diagnostics),
    )


__all__ = [
    "_StreamConsumerSpawnContext",
    "_SubprocessExecution",
    "_build_stream_config",
    "_create_stream_callback",
    "_execute_subprocess",
    "_run_subprocess_with_streams",
    "_run_subprocess_without_streams",
    "_spawn_stream_consumers",
    "_spawn_subprocess",
]
