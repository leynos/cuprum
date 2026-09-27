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

from cuprum.context import EnvMode, UnsetType, _validate_timeout

type _EnvMapping = cabc.Mapping[str, str | UnsetType] | None
type _CwdType = str | Path | None

_DEFAULT_CANCEL_GRACE = 0.5
_DEFAULT_NATIVE_PUMP_CLEANUP_GRACE = 0.5


_DEFAULT_ENCODING = "utf-8"
_DEFAULT_ERROR_HANDLING = "replace"

__all__ = [
    "ExecutionContext",
    "StdinInput",
    "StdinSource",
    "StdinSourceError",
    "StdinStream",
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
    env_mode:
        Environment policy applied when rendering ``env`` for the subprocess.

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
    env_mode: EnvMode = EnvMode.OVERLAY

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


class StdinSourceError(Exception):
    """Raised when a streaming stdin producer or its encoder fails.

    A caller who supplies a :class:`StdinStream` sees failures from three
    sources wrapped in this type: the producer raising while being pulled, the
    producer yielding a value that is neither ``str`` nor ``bytes``, and the
    incremental encoder rejecting a ``str`` chunk. The original exception is
    chained as ``__cause__``, so the caller can still inspect it.

    The child is terminated before this is raised, and the run's stdin
    writer and pipe are finalized, so catching this type is enough to know
    that no writer outlives the run. Cancellation is *not* wrapped: an
    ``asyncio.CancelledError`` propagates unchanged, because it is a
    control-flow signal rather than a source failure.
    """


type StdinSource = StdinInput | StdinStream
"""Either a complete payload or a streaming producer."""


@dc.dataclass(frozen=True, slots=True)
class StdinStream:
    """A library-owned, bounded, pull-after-drain producer for stdin.

    Cuprum pulls one chunk from *chunks*, writes it to the child's stdin pipe,
    and waits for that write to drain before pulling the next. Peak memory is
    therefore bounded by the largest single chunk rather than by the whole
    payload, which is the point of the type: a caller can feed a child more
    data than they would ever hold in one buffer.

    The producer is advanced with ``aiter()``, so *chunks* may be any async
    iterable. ``str`` chunks are encoded with the run's
    :attr:`ExecutionContext.encoding` and :attr:`ExecutionContext.errors`,
    incrementally, so a multi-byte character split across two chunks is
    still encoded correctly. ``bytes`` chunks are written verbatim.

    Cuprum owns the producer for the duration of the run. On every exit path
    -- normal completion, producer failure, exceeding the timeout, and
    cancellation -- it calls the iterator's ``aclose()`` when the producer
    provides one, closes the pipe, and awaits ``wait_closed()``. A caller
    therefore does not need to finalize their own generator; a caller must
    not yield from a generator that something else is concurrently driving.

    A child that closes its stdin early (``head`` is the canonical example)
    is normal rather than an error: the resulting ``BrokenPipeError`` is
    recorded as a ``stdin_error`` observation and the run continues to its
    exit code. Only producer and encoder failures raise
    :class:`StdinSourceError`.
    """

    chunks: cabc.AsyncIterable[str | bytes] | cabc.AsyncIterator[str | bytes]
    """The async iterable or iterator whose chunks become the child's stdin."""


def _resolve_stdin_source(
    stdin: StdinSource | None,
    ctx: ExecutionContext,
) -> bytes | StdinStream | None:
    """Resolve a caller's ``stdin=`` argument into what the spawn path needs.

    A payload resolves here, exactly as it always has, so the encoding error
    surfaces synchronously from ``run()`` rather than from inside the writer
    task. A stream stays unresolved: pulling it is the writer's job, and
    pulling it earlier would defeat the bound.

    Parameters
    ----------
    stdin : StdinSource | None
        The caller's argument, or ``None`` to inherit the parent's stdin.
    ctx : ExecutionContext
        The context whose encoding and error handling resolve a ``text``
        payload.

    Returns
    -------
    bytes | StdinStream | None
        The encoded payload, the stream to pull, or ``None`` for inherited
        stdin.
    """
    if stdin is None:
        return None
    if isinstance(stdin, StdinStream):
        return stdin
    return stdin.resolve(ctx)
