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

if typ.TYPE_CHECKING:
    from cuprum.program import Program

type _ArgValue = str | int | float | bool | Path
type SafeCmdBuilder = cabc.Callable[..., SafeCmd]
type _EnvMapping = cabc.Mapping[str, str] | None
type _CwdType = str | Path | None

_READ_SIZE = 4096
_DEFAULT_CANCEL_GRACE = 0.5


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
    """Structured result returned by command execution."""

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
    """Execution parameters for SafeCmd runtime control."""

    env: _EnvMapping = None
    cwd: _CwdType = None
    cancel_grace: float = _DEFAULT_CANCEL_GRACE


@dc.dataclass(frozen=True, slots=True)
class SafeCmd:
    """Typed representation of a curated command ready for execution."""

    program: Program
    argv: tuple[str, ...]
    project: ProjectSettings

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

        ``capture`` controls whether stdout/stderr are retained; when disabled
        the returned result contains ``None`` for the respective fields.
        ``echo`` mirrors stdout/stderr to the parent process while still
        respecting the ``capture`` flag. ``context`` carries runtime settings
        for environment overlays, working directory, and cancellation grace.
        """
        ctx = context or ExecutionContext()

        resolved_env = _merge_env(ctx.env)
        stdout_target = (
            asyncio.subprocess.PIPE if capture or echo else asyncio.subprocess.DEVNULL
        )
        stderr_target = (
            asyncio.subprocess.PIPE if capture or echo else asyncio.subprocess.DEVNULL
        )

        process = await asyncio.create_subprocess_exec(
            *self.argv_with_program,
            stdout=stdout_target,
            stderr=stderr_target,
            env=resolved_env,
            cwd=str(ctx.cwd) if ctx.cwd is not None else None,
        )

        stdout_task = asyncio.create_task(
            _consume_stream(
                process.stdout,
                capture_output=capture,
                echo_output=echo,
                sink=sys.stdout,
            ),
        )
        stderr_task = asyncio.create_task(
            _consume_stream(
                process.stderr,
                capture_output=capture,
                echo_output=echo,
                sink=sys.stderr,
            ),
        )

        try:
            exit_code = await process.wait()
        except asyncio.CancelledError:
            await _terminate_process(process, ctx.cancel_grace)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise

        stdout_text, stderr_text = await asyncio.gather(
            stdout_task,
            stderr_task,
        )

        return CommandResult(
            program=self.program,
            argv=self.argv,
            exit_code=exit_code,
            pid=process.pid or -1,
            stdout=stdout_text,
            stderr=stderr_text,
        )


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


def _merge_env(extra: _EnvMapping) -> dict[str, str] | None:
    """Overlay extra environment variables when provided."""
    if extra is None:
        return None
    merged = os.environ.copy()
    merged.update(extra)
    return merged


async def _consume_stream(
    stream: asyncio.StreamReader | None,
    *,
    capture_output: bool,
    echo_output: bool,
    sink: typ.IO[str],
) -> str | None:
    """Read from a subprocess stream, teeing to sink when requested."""
    if stream is None:
        return "" if capture_output else None

    buffer = bytearray() if capture_output else None
    while True:
        chunk = await stream.read(_READ_SIZE)
        if not chunk:
            break
        if buffer is not None:
            buffer.extend(chunk)
        if echo_output:
            _write_chunk(sink, chunk)

    if buffer is None:
        return None
    return buffer.decode("utf-8", errors="replace")


def _write_chunk(sink: typ.IO[str], chunk: bytes) -> None:
    """Write a bytes chunk to a text sink without blocking the event loop."""
    buffer = getattr(sink, "buffer", None)
    if buffer is not None:
        buffer.write(chunk)
        buffer.flush()
        return
    text = chunk.decode("utf-8", errors="replace")
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
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), grace_period)
    except TimeoutError:
        process.kill()
        await process.wait()


__all__ = [
    "CommandResult",
    "ExecutionContext",
    "SafeCmd",
    "SafeCmdBuilder",
    "UnknownProgramError",
    "make",
]
