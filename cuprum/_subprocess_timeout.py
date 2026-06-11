"""Subprocess timeout translation and exit-event accounting.

Owns the timeout dataclasses and errors, the translation of internal
timeouts into the public :class:`~cuprum.sh.TimeoutExpired`, and the
exit-event helpers shared by the timeout and normal completion paths.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import time
import typing as typ

from cuprum._pipeline_internals import _EventDetails
from cuprum._subprocess_context import _sh_module

if typ.TYPE_CHECKING:
    from cuprum._pipeline_internals import _StageObservation
    from cuprum._subprocess_execution import _SubprocessExecution


def _require_timeout(timeout: float | None, exc: BaseException) -> float:
    """Return ``timeout`` or fail loudly when none was configured.

    A ``TimeoutError`` reaching the timeout handlers without a configured
    timeout indicates an internal inconsistency; this is the single home for
    that guard.
    """
    if timeout is None:
        msg = "TimeoutError without a configured timeout"
        raise RuntimeError(msg) from exc
    return timeout


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
        """Store captured output and timing from the timed-out subprocess."""
        super().__init__(f"Execution exceeded {details.timeout}s timeout")
        self.timeout = details.timeout
        self.stdout = details.stdout
        self.stderr = details.stderr
        self.exited_at = details.exited_at


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
        timeout = _require_timeout(ctx.execution.timeout, exc)
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
    raise _SubprocessTimeoutError(
        _SubprocessTimeoutDetails(
            timeout=_require_timeout(timeout, exc),
            stdout=stdout_text,
            stderr=stderr_text,
            exited_at=time.perf_counter(),
        ),
    ) from exc


__all__ = [
    "_ExitEventDetails",
    "_SubprocessTimeoutContext",
    "_SubprocessTimeoutDetails",
    "_SubprocessTimeoutError",
    "_TimeoutContext",
    "_emit_exit_event",
    "_get_exit_code",
    "_handle_stream_timeout",
    "_handle_subprocess_timeout",
    "_raise_timeout_expired",
    "_require_timeout",
]
