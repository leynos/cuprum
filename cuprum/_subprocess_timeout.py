"""Subprocess timeout translation and exit-event accounting.

Owns the timeout dataclasses and errors, the translation of internal
timeouts into the public :class:`~cuprum.sh.TimeoutExpired`, and the
exit-event helpers shared by the timeout and normal completion paths.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import logging
import time
import typing as typ

from cuprum._pipeline_internals import _EventDetails
from cuprum._subprocess_context import _sh_module

if typ.TYPE_CHECKING:
    from cuprum._pipeline_internals import _StageObservation
    from cuprum._subprocess_execution import _SubprocessExecution

# Timeout diagnostics ride the module-scoped ``cuprum.timeout`` logger,
# mirroring the ``cuprum.stdin`` convention in ``cuprum._subprocess_stdin``:
# structured ``cuprum_*`` extra fields let observability integrations key on
# stable attributes rather than parsing the human-readable message.
_LOGGER = logging.getLogger("cuprum.timeout")

# Distinguishes an elapsed wall-clock deadline from an immediate expiry forced
# by a non-positive (``<= 0``) timeout, which cannot await the process at all.
# These are the stable ``timeout_mode`` values on both the ``cuprum.timeout``
# log records and the ``timeout`` observe event.
type _TimeoutMode = typ.Literal["elapsed_deadline", "non_positive_immediate"]


def _emit_timeout_log(
    level: int,
    msg: str,
    *args: object,
    extra: dict[str, object],
) -> None:
    """Emit a timeout diagnostic that can never mask the timeout it describes.

    Telemetry is strictly best-effort here: a logging failure (for example a
    misconfigured handler that raises) must not replace the ``TimeoutError`` or
    ``TimeoutExpired`` the caller is about to raise, nor abort teardown, so any
    exception raised while logging is swallowed. Preserving cleanup and
    exception precedence outranks emitting the diagnostic.
    """
    with contextlib.suppress(Exception):
        _LOGGER.log(level, msg, *args, extra=extra)


def _log_timeout_expiry(
    *,
    pid: int | None,
    configured_timeout: float,
    mode: _TimeoutMode,
) -> None:
    """Record a structured diagnostic when a subprocess timeout expires.

    ``mode`` distinguishes an elapsed wall-clock deadline
    (``"elapsed_deadline"``) from the deterministic immediate expiry taken for a
    non-positive timeout (``"non_positive_immediate"``).
    """
    _emit_timeout_log(
        logging.WARNING,
        "subprocess_timeout_expired pid=%s timeout=%s mode=%s",
        pid,
        configured_timeout,
        mode,
        extra={
            "cuprum_pid": pid,
            "cuprum_operation": "wait",
            "cuprum_timeout_s": configured_timeout,
            "cuprum_timeout_mode": mode,
            "cuprum_error_type": TimeoutError.__name__,
        },
    )


def _log_teardown_drain_failure(
    *,
    pid: int | None,
    error_types: tuple[str, ...],
) -> None:
    """Record a structured diagnostic when teardown drains a failing consumer.

    Called when cancelling and draining stream consumers surfaces an unexpected
    exception (anything other than the expected ``CancelledError``). Reports the
    teardown outcome so a drain failure is observable even though it is absorbed
    to preserve the primary timeout or cancellation.
    """
    joined = ",".join(error_types)
    _emit_timeout_log(
        logging.ERROR,
        "subprocess_teardown_drain_failed pid=%s errors=%s",
        pid,
        joined,
        extra={
            "cuprum_pid": pid,
            "cuprum_operation": "teardown",
            "cuprum_teardown_outcome": "drain_error",
            "cuprum_error_type": joined,
        },
    )


def _safe_emit(
    observation: _StageObservation,
    phase: typ.Literal["timeout", "teardown_error"],
    details: _EventDetails,
) -> None:
    """Emit an observe event best-effort so telemetry cannot mask a failure.

    An observe-hook failure — which :meth:`_StageObservation.emit` re-raises —
    must never replace the ``TimeoutExpired`` / ``CancelledError`` the caller is
    about to raise, nor abort teardown. ``emit`` records any scheduled
    async-hook tasks on the observation *before* raising, so those are still
    drained by the runner; only the synchronous hook failure is swallowed here.
    Preserving the primary exception and cleanup precedence outranks emitting
    the observe event.
    """
    # Swallow a synchronous observe-hook failure (including a hook raising
    # CancelledError) so it cannot replace the timeout/cancellation.
    with contextlib.suppress(Exception, asyncio.CancelledError):
        observation.emit(phase, details)


def _emit_timeout_event(
    observation: _StageObservation,
    *,
    pid: int | None,
    configured_timeout: float,
    mode: _TimeoutMode,
) -> None:
    """Best-effort ``timeout`` observe event describing an expiry.

    Carries the stable fields callers use to distinguish an elapsed deadline
    from an immediate non-positive expiry: ``operation="wait"``, ``pid``,
    ``timeout_s`` (the configured timeout), ``error_type="TimeoutError"``, and
    ``timeout_mode``. Emitted before the preserved ``exit`` event and
    ``TimeoutExpired``.
    """
    _safe_emit(
        observation,
        "timeout",
        _EventDetails(
            pid=pid,
            operation="wait",
            error_type=TimeoutError.__name__,
            timeout_s=configured_timeout,
            timeout_mode=mode,
        ),
    )


def _emit_teardown_error_event(
    observation: _StageObservation,
    *,
    pid: int | None,
    error_types: tuple[str, ...],
) -> None:
    """Best-effort ``teardown_error`` observe event for a consumer drain failure.

    Carries ``operation="drain"``, ``pid``, and ``error_type`` (the comma-joined
    failure classes). The failure is absorbed to preserve the primary timeout or
    cancellation, but stays observable through this event.
    """
    joined = ",".join(error_types)
    _safe_emit(
        observation,
        "teardown_error",
        _EventDetails(
            pid=pid,
            operation="drain",
            error_type=joined,
            note=f"consumer drain failed: {joined}",
        ),
    )


class _SubprocessInvariantError(RuntimeError):
    """Raised when an internal subprocess-execution invariant is violated.

    Subclasses :class:`RuntimeError` so it is still caught by broad runtime
    handlers, while giving these impossible-state guards a distinct,
    package-scoped type that callers can tell apart from unrelated runtime
    failures.
    """


def _require_timeout(timeout: float | None, exc: BaseException) -> float:
    """Return ``timeout`` or fail loudly when none was configured.

    A ``TimeoutError`` reaching the timeout handlers without a configured
    timeout indicates an internal inconsistency; this is the single home for
    that guard.
    """
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
    """Resolve the timeout payload from either timeout variant.

    This is the pure timeout-payload seam behind
    [`_handle_subprocess_timeout`][cuprum._subprocess_timeout._handle_subprocess_timeout].
    A [`_SubprocessTimeoutError`][cuprum._subprocess_timeout._SubprocessTimeoutError]
    already carries a captured payload from the stream-timeout path, so it is
    used verbatim. A bare ``TimeoutError`` is resolved from ``fallback`` — whose
    configured timeout must be present (a missing one is an internal invariant
    violation). Either branch yields a concrete ``timeout``, so the resulting
    ``TimeoutExpired`` report is consistent regardless of which path timed out.
    """
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
    before it fired. ``timeout`` is carried metadata, not a deadline this
    function awaits on; it records the timeout the caller configured so it can
    populate the raised error. The function is synchronous, so ``ASYNC109`` does
    not apply to that parameter name.
    """
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
    "_TimeoutMode",
    "_emit_exit_event",
    "_emit_teardown_error_event",
    "_emit_timeout_event",
    "_get_exit_code",
    "_handle_stream_timeout",
    "_handle_subprocess_timeout",
    "_log_teardown_drain_failure",
    "_log_timeout_expiry",
    "_raise_timeout_expired",
    "_require_timeout",
    "_resolve_timeout_payload",
]
