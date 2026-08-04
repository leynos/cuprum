"""Subprocess timeout translation and exit-event accounting.

Owns the timeout dataclasses and errors, the translation of internal
timeouts into the public :class:`~cuprum.sh.TimeoutExpired`, and the
exit-event helpers shared by the timeout and normal completion paths.
"""

from __future__ import annotations

import dataclasses as dc
import time
import typing as typ

from cuprum._pipeline_internals import _EventDetails
from cuprum._pipeline_types import _ExecutionInvariantError
from cuprum._subprocess_context import _sh_module

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._pipeline_internals import _StageObservation
    from cuprum._subprocess_execution import _SubprocessExecution


class _SubprocessInvariantError(_ExecutionInvariantError):
    """Raised when an internal subprocess-execution invariant is violated.

    Subclasses the shared package-level invariant error, which itself derives
    from :class:`RuntimeError`, while retaining a distinct type for subprocess
    failures.
    """


def _require_timeout(timeout: float | None, exc: BaseException) -> float:
    """Return ``timeout`` or fail loudly when none was configured."""
    if timeout is None:
        msg = "TimeoutError without a configured timeout"
        raise _SubprocessInvariantError(msg) from exc
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


@dc.dataclass(frozen=True, slots=True)
class _TimeoutFallback:
    """Fields for resolving a bare ``TimeoutError`` into a timeout payload.

    The stream-timeout path captures its own payload, but a bare
    ``TimeoutError`` carries none, so these are the caller-supplied fallbacks:
    the configured timeout, the captured stdout/stderr, and the clock reading
    taken as the exit time.
    """

    configured_timeout: float | None
    stdout: str | None
    stderr: str | None
    exited_at: float


def _resolve_timeout_payload(
    exc: TimeoutError | _SubprocessTimeoutError,
    fallback: _TimeoutFallback,
) -> _SubprocessTimeoutDetails:
    """Resolve the timeout payload from either timeout variant."""
    # This is the pure timeout-payload seam behind _handle_subprocess_timeout.
    # A _SubprocessTimeoutError already carries a captured payload from the
    # stream-timeout path, so it is used verbatim. A bare TimeoutError is
    # resolved from ``fallback`` — whose configured timeout must be present (a
    # missing one is an internal invariant violation). Either branch yields a
    # concrete ``timeout``, so the resulting TimeoutExpired report is
    # consistent regardless of which path timed out.
    match exc:
        case _SubprocessTimeoutError(
            timeout=timeout,
            stdout=stdout,
            stderr=stderr,
            exited_at=exited_at,
        ):
            return _SubprocessTimeoutDetails(
                timeout=timeout,
                stdout=stdout,
                stderr=stderr,
                exited_at=exited_at,
            )
        case _:
            return _SubprocessTimeoutDetails(
                timeout=_require_timeout(fallback.configured_timeout, exc),
                stdout=fallback.stdout,
                stderr=fallback.stderr,
                exited_at=fallback.exited_at,
            )


def _handle_subprocess_timeout(
    ctx: _SubprocessTimeoutContext,
    exc: TimeoutError | _SubprocessTimeoutError,
) -> typ.NoReturn:
    """Handle a subprocess timeout by emitting exit event and raising TimeoutExpired."""
    payload = _resolve_timeout_payload(
        exc,
        _TimeoutFallback(
            configured_timeout=ctx.execution.timeout,
            stdout=ctx.stdout_text,
            stderr=ctx.stderr_text,
            exited_at=time.perf_counter(),
        ),
    )

    exit_code = _get_exit_code(ctx.process)
    _emit_exit_event(
        ctx.execution.observation,
        _ExitEventDetails(
            pid=ctx.process.pid,
            exit_code=exit_code,
            started_at=ctx.started_at,
            exited_at=payload.exited_at,
        ),
    )
    _raise_timeout_expired(
        _TimeoutContext(
            cmd_argv=ctx.execution.cmd.argv_with_program,
            timeout=payload.timeout,
            stdout=payload.stdout,
            stderr=payload.stderr,
        ),
        exc,
    )


def _handle_stream_timeout(
    exc: TimeoutError,
    *,
    stdout_text: str | None,
    stderr_text: str | None,
    timeout: float | None,
) -> typ.NoReturn:
    """Raise ``_SubprocessTimeoutError`` carrying pre-drained stream output.

    The caller drains the stream consumers exactly once and passes the decoded
    stdout/stderr here, so a timeout preserves whatever output was captured
    before it fired.

    Raises
    ------
    _SubprocessInvariantError
        If no timeout was configured, signalling an internal inconsistency.
    _SubprocessTimeoutError
        Wrapping the captured stdout/stderr once the timeout is resolved.
    """  # noqa: DOC502 - _SubprocessInvariantError propagates from _require_timeout
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
    "_SubprocessInvariantError",
    "_SubprocessTimeoutContext",
    "_SubprocessTimeoutDetails",
    "_SubprocessTimeoutError",
    "_TimeoutContext",
    "_TimeoutFallback",
    "_emit_exit_event",
    "_get_exit_code",
    "_handle_stream_timeout",
    "_handle_subprocess_timeout",
    "_raise_timeout_expired",
    "_require_timeout",
    "_resolve_timeout_payload",
]
