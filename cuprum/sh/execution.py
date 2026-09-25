"""Execution context, timeout, and stdin types for ``cuprum.sh``.

These types describe how a command or pipeline stage is run rather than what
it does: the environment overlay, working directory, cancellation grace
periods, echo sinks, and encoding used to run it, alongside the timeout
exception and stdin payload types that accompany execution. The
``cuprum.sh`` package re-exports them.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import typing as typ
from pathlib import Path

from cuprum.context import _validate_timeout

type _EnvMapping = cabc.Mapping[str, str] | None
type _CwdType = str | Path | None

_DEFAULT_CANCEL_GRACE = 0.5
_DEFAULT_NATIVE_PUMP_CLEANUP_GRACE = 0.5


_DEFAULT_ENCODING = "utf-8"
_DEFAULT_ERROR_HANDLING = "replace"

__all__ = [
    "ExecutionContext",
    "StdinInput",
    "TimeoutExpired",
]


@dc.dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Execution parameters for SafeCmd runtime control.

    Attributes
    ----------
    env:
        Environment variable overlay applied to the subprocess.
    cwd:
        Working directory for the subprocess.
    cancel_grace:
        Seconds to wait after SIGTERM before escalating to SIGKILL.
    native_pump_cleanup_grace:
        Seconds to wait for a cancelled native-pump worker before its
        descriptor cleanup is deferred to its completion callback.
    timeout:
        Optional runtime timeout in seconds. ``None`` means no override.
    stdout_sink:
        Text sink for echoing stdout; defaults to the active ``sys.stdout``.
    stderr_sink:
        Text sink for echoing stderr; defaults to the active ``sys.stderr``.
        When no ``on_idle`` callback is supplied, it also receives the idle
        heartbeat's keepalive line, written and flushed synchronously on the
        run's event loop, so its ``write`` and ``flush`` must return promptly:
        a sink that blocks delays the run's stream reads, timeout handling,
        and cancellation. Hand a slow destination to a worker thread, an
        executor, or a genuinely non-blocking drain such as a queue fed with
        ``put_nowait``. A separate asyncio task on the run's own loop is not
        enough: draining that queue still competes with the parent's stream
        reads.
    encoding:
        Character encoding used when decoding subprocess output.
    errors:
        Error handling strategy applied during decoding.
    tags:
        Optional metadata attached to structured execution events.

    """

    env: _EnvMapping = None
    cwd: _CwdType = None
    cancel_grace: float = _DEFAULT_CANCEL_GRACE
    native_pump_cleanup_grace: float = _DEFAULT_NATIVE_PUMP_CLEANUP_GRACE
    timeout: float | None = None
    stdout_sink: typ.IO[str] | None = None
    stderr_sink: typ.IO[str] | None = None
    encoding: str = _DEFAULT_ENCODING
    errors: str = _DEFAULT_ERROR_HANDLING
    tags: cabc.Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        """Validate the native-pump cleanup grace after initialization."""
        cleanup_grace = _validate_timeout(
            self.native_pump_cleanup_grace,
            "ExecutionContext native_pump_cleanup_grace",
        )
        if cleanup_grace is None:
            msg = "ExecutionContext native_pump_cleanup_grace must not be None"
            raise ValueError(msg)
        object.__setattr__(self, "native_pump_cleanup_grace", cleanup_grace)


class TimeoutExpired(TimeoutError):  # ruff: ignore[error-suffix-on-exception-name] - match subprocess.TimeoutExpired naming.
    """Raised when command execution exceeds the configured timeout."""

    def __init__(
        self,
        *,
        cmd: cabc.Sequence[str] | object,
        timeout: float,
        output: str | bytes | None = None,
        stderr: str | bytes | None = None,
    ) -> None:
        """Store the command, timeout, and any captured output."""
        super().__init__(f"Command {cmd!r} timed out after {timeout} seconds")
        self.cmd = cmd
        self.timeout = timeout
        self.output = output
        self.stderr = stderr

    @property
    def stdout(self) -> str | bytes | None:
        """Captured stdout, mirroring ``subprocess.TimeoutExpired``.

        Returns
        -------
        str | bytes | None
            Captured standard output, or ``None`` when no output was
            captured before expiry.
        """
        return self.output


@dc.dataclass(frozen=True, slots=True)
class StdinInput:
    """Caller-provided data to write to a subprocess's stdin pipe.

    Exactly one of *text* or *data* may be supplied.
    """

    text: str | None = None
    data: bytes | None = None

    def __post_init__(self) -> None:
        """Reject ambiguous stdin payloads."""
        if self.text is not None and self.data is not None:
            msg = "text and data cannot both be provided"
            raise ValueError(msg)

    def resolve(self, ctx: ExecutionContext) -> bytes | None:
        """Return the bytes payload, encoding *text* with *ctx* when needed.

        Parameters
        ----------
        ctx : ExecutionContext
            The execution context whose ``encoding`` and ``errors`` encode
            ``text`` when no raw ``data`` is set.

        Returns
        -------
        bytes | None
            The raw *data* payload, or *text* encoded with ``ctx.encoding``
            and ``ctx.errors``; ``None`` when neither field is set.

        Raises
        ------
        UnicodeEncodeError
            If ``text`` cannot be encoded with ``ctx.encoding`` under
            ``ctx.errors`` (for example, ``errors="strict"``).
        """  # ruff: ignore[docstring-extraneous-exception] - UnicodeEncodeError propagates from str.encode
        if self.text is not None:
            return self.text.encode(ctx.encoding, ctx.errors)
        return self.data
