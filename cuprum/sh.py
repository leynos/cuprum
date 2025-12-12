"""Safe command construction and execution facade for curated programs.

This module focuses on the typed core: building ``SafeCmd`` instances from
curated ``Program`` values and providing a minimal async runtime for executing
them with predictable semantics.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import dataclasses as dc
import os
import sys
import typing as typ
from pathlib import Path

from cuprum.catalogue import (
    DEFAULT_CATALOGUE,
    ProgramCatalogue,
    ProjectSettings,
)
from cuprum.catalogue import UnknownProgramError as UnknownProgramError
from cuprum.context import current_context

if typ.TYPE_CHECKING:
    from cuprum.program import Program

type _ArgValue = str | int | float | bool | Path
type SafeCmdBuilder = cabc.Callable[..., SafeCmd]
type _EnvMapping = cabc.Mapping[str, str] | None
type _CwdType = str | Path | None

_READ_SIZE = 4096
_DEFAULT_CANCEL_GRACE = 0.5
_DEFAULT_ENCODING = "utf-8"
_DEFAULT_ERROR_HANDLING = "replace"


def _stringify_arg(value: _ArgValue) -> str:
    """Convert values into argv-safe strings.

    ``None`` is disallowed because it is almost always a mistake in CLI argv
    construction. Callers should decide how to represent missing values (for
    example, omit the flag) before invoking ``sh.make``.
    """
    if value is None:
        msg = "None is not a valid argv element for sh.make"
        raise TypeError(msg)
    return str(value)


def _serialize_kwargs(kwargs: dict[str, _ArgValue]) -> tuple[str, ...]:
    """Serialise keyword arguments to CLI-style ``--flag=value`` entries."""
    flags: list[str] = []
    for key, value in kwargs.items():
        normalized_key = key.replace("_", "-")
        flags.append(f"--{normalized_key}={_stringify_arg(value)}")
    return tuple(flags)


def _coerce_argv(
    args: tuple[_ArgValue, ...],
    kwargs: dict[str, _ArgValue],
) -> tuple[str, ...]:
    """Convert positional and keyword arguments into a single argv tuple."""
    positional = tuple(_stringify_arg(arg) for arg in args)
    flags = _serialize_kwargs(kwargs)
    return positional + flags


@dc.dataclass(frozen=True, slots=True)
class CommandResult:
    """Structured result returned by command execution.

    Attributes
    ----------
    program:
        Program that was executed.
    argv:
        Argument vector (excluding the program name) passed to the process.
    exit_code:
        Exit status reported by the process.
    pid:
        Process identifier; ``-1`` when unavailable.
    stdout:
        Captured standard output, or ``None`` when capture was disabled.
    stderr:
        Captured standard error, or ``None`` when capture was disabled.

    """

    program: Program
    argv: tuple[str, ...]
    exit_code: int
    pid: int
    stdout: str | None
    stderr: str | None

    @property
    def ok(self) -> bool:
        """Return True when the command exited successfully."""
        return self.exit_code == 0


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
    stdout_sink:
        Text sink for echoing stdout; defaults to the active ``sys.stdout``.
    stderr_sink:
        Text sink for echoing stderr; defaults to the active ``sys.stderr``.
    encoding:
        Character encoding used when decoding subprocess output.
    errors:
        Error handling strategy applied during decoding.

    """

    env: _EnvMapping = None
    cwd: _CwdType = None
    cancel_grace: float = _DEFAULT_CANCEL_GRACE
    stdout_sink: typ.IO[str] | None = None
    stderr_sink: typ.IO[str] | None = None
    encoding: str = _DEFAULT_ENCODING
    errors: str = _DEFAULT_ERROR_HANDLING


@dc.dataclass(frozen=True, slots=True)
class _StreamConfig:
    """Configuration for decoding and echoing a subprocess stream."""

    capture_output: bool
    echo_output: bool
    sink: typ.IO[str]
    encoding: str
    errors: str


@dc.dataclass(frozen=True, slots=True)
class SafeCmd:
    """Typed representation of a curated command ready for execution."""

    program: Program
    argv: tuple[str, ...]
    project: ProjectSettings
    __weakref__: object = dc.field(
        init=False,
        repr=False,
        hash=False,
        compare=False,
    )

    @property
    def argv_with_program(self) -> tuple[str, ...]:
        """Return argv prefixed with the program name."""
        return (str(self.program), *self.argv)

    async def run(
        self,
        *,
        capture: bool = True,
        echo: bool = False,
        context: ExecutionContext | None = None,
    ) -> CommandResult:
        """Execute the command asynchronously with predictable cancellation.

        Parameters
        ----------
        capture:
            When ``True`` capture stdout/stderr; otherwise discard them.
        echo:
            When ``True`` tee stdout/stderr to the parent process.
        context:
            Optional execution settings such as env, cwd, and cancel grace.

        Returns
        -------
        CommandResult
            Structured information about the completed process.

        Raises
        ------
        ForbiddenProgramError
            If the program is not in the current context's allowlist.

        """
        after_hooks = _run_before_hooks(self)
        ctx = context or ExecutionContext()
        stdout_sink = ctx.stdout_sink if ctx.stdout_sink is not None else sys.stdout

        process = await asyncio.create_subprocess_exec(
            *self.argv_with_program,
            stdout=(
                asyncio.subprocess.PIPE
                if capture or echo
                else asyncio.subprocess.DEVNULL
            ),
            stderr=(
                asyncio.subprocess.PIPE
                if capture or echo
                else asyncio.subprocess.DEVNULL
            ),
            env=_merge_env(ctx.env),
            cwd=str(ctx.cwd) if ctx.cwd is not None else None,
        )

        if not capture and not echo:
            try:
                exit_code = await process.wait()
            except asyncio.CancelledError:
                await _terminate_process(process, ctx.cancel_grace)
                raise
            result = CommandResult(
                program=self.program,
                argv=self.argv,
                exit_code=exit_code,
                pid=process.pid if process.pid is not None else -1,
                stdout=None,
                stderr=None,
            )
            # Execute after hooks (LIFO order - stored prepended)
            for hook in after_hooks:
                hook(self, result)
            return result

        stream_config = _StreamConfig(
            capture_output=capture,
            echo_output=echo,
            sink=stdout_sink,
            encoding=ctx.encoding,
            errors=ctx.errors,
        )
        consumers = (
            asyncio.create_task(
                _consume_stream(
                    process.stdout,
                    stream_config,
                ),
            ),
            asyncio.create_task(
                _consume_stream(
                    process.stderr,
                    dc.replace(
                        stream_config,
                        sink=(
                            ctx.stderr_sink
                            if ctx.stderr_sink is not None
                            else sys.stderr
                        ),
                    ),
                ),
            ),
        )

        try:
            exit_code = await process.wait()
        except asyncio.CancelledError:
            await _terminate_process(process, ctx.cancel_grace)
            await asyncio.gather(*consumers, return_exceptions=True)
            raise

        stdout_text, stderr_text = await asyncio.gather(*consumers)

        result = CommandResult(
            program=self.program,
            argv=self.argv,
            exit_code=exit_code,
            pid=process.pid if process.pid is not None else -1,
            stdout=stdout_text,
            stderr=stderr_text,
        )
        # Execute after hooks (LIFO order - stored prepended)
        for hook in after_hooks:
            hook(self, result)
        return result

    def run_sync(
        self,
        *,
        capture: bool = True,
        echo: bool = False,
        context: ExecutionContext | None = None,
    ) -> CommandResult:
        """Execute the command synchronously with predictable semantics.

        This method mirrors ``run()`` by driving the event loop internally.
        All parameters and return semantics are identical.

        Parameters
        ----------
        capture:
            When ``True`` capture stdout/stderr; otherwise discard them.
        echo:
            When ``True`` tee stdout/stderr to the parent process.
        context:
            Optional execution settings such as env, cwd, and cancel grace.

        Returns
        -------
        CommandResult
            Structured information about the completed process.

        """
        return asyncio.run(self.run(capture=capture, echo=echo, context=context))


def make(
    program: Program,
    *,
    catalogue: ProgramCatalogue = DEFAULT_CATALOGUE,
) -> SafeCmdBuilder:
    """Build a callable that produces ``SafeCmd`` instances for ``program``.

    The supplied ``program`` must exist in the provided catalogue; otherwise an
    ``UnknownProgramError`` is raised to keep the allowlist the default gate.
    """
    entry = catalogue.lookup(program)

    def builder(*args: _ArgValue, **kwargs: _ArgValue) -> SafeCmd:
        argv = _coerce_argv(args, kwargs)
        return SafeCmd(program=entry.program, argv=argv, project=entry.project)

    return builder


if typ.TYPE_CHECKING:
    from cuprum.context import AfterHook


def _run_before_hooks(cmd: SafeCmd) -> tuple[AfterHook, ...]:
    """Check allowlist, run before hooks, and return after hooks for later.

    This helper validates the command against the current context's allowlist,
    executes all registered before hooks in FIFO order, and returns the after
    hooks tuple for invocation after command completion.
    """
    ctx = current_context()
    ctx.check_allowed(cmd.program)
    for hook in ctx.before_hooks:
        hook(cmd)
    return ctx.after_hooks


def _merge_env(extra: _EnvMapping) -> dict[str, str] | None:
    """Overlay extra environment variables when provided."""
    if extra is None:
        return None
    merged = os.environ.copy()
    merged |= extra
    return merged


async def _consume_stream(
    stream: asyncio.StreamReader | None,
    config: _StreamConfig,
) -> str | None:
    """Read from a subprocess stream, teeing to sink when requested."""
    if stream is None:
        return "" if config.capture_output else None

    buffer = bytearray() if config.capture_output else None
    while True:
        chunk = await stream.read(_READ_SIZE)
        if not chunk:
            break
        if buffer is not None:
            buffer.extend(chunk)
        if config.echo_output:
            _write_chunk(
                config.sink,
                chunk,
                encoding=config.encoding,
                errors=config.errors,
            )

    if buffer is None:
        return None
    return buffer.decode(config.encoding, errors=config.errors)


def _write_chunk(
    sink: typ.IO[str],
    chunk: bytes,
    *,
    encoding: str,
    errors: str,
) -> None:
    """Write a bytes chunk to a text sink synchronously, avoiding extra encoding.

    For stdio echo this blocking write is acceptable; future slow-sink handling
    can layer on a background writer if needed.
    """
    buffer = getattr(sink, "buffer", None)
    if buffer is not None:
        buffer.write(chunk)
        buffer.flush()
        return
    text = chunk.decode(encoding, errors=errors)
    sink.write(text)
    sink.flush()


async def _terminate_process(
    process: asyncio.subprocess.Process,
    grace_period: float,
) -> None:
    """Terminate a running process, escalating to kill after the grace period."""
    grace_period = max(0.0, grace_period)
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(process.wait(), grace_period)
    except asyncio.TimeoutError:  # noqa: UP041 - explicit asyncio timeout needed
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            return
        await process.wait()


__all__ = [
    "CommandResult",
    "ExecutionContext",
    "SafeCmd",
    "SafeCmdBuilder",
    "UnknownProgramError",
    "make",
]
