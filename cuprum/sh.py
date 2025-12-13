"""Safe command construction and execution facade for curated programs.

This module focuses on the typed core: building ``SafeCmd`` instances from
curated ``Program`` values and providing a minimal async runtime for executing
them with predictable semantics.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import contextlib
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
_MIN_PIPELINE_STAGES = 2


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
class PipelineResult:
    """Structured result returned by pipeline execution.

    Attributes
    ----------
    stages:
        Command results for each pipeline stage, in execution order. For stages
        whose stdout is streamed into the next stage, ``stdout`` is ``None``.
        The final stage carries captured stdout when enabled.

    """

    stages: tuple[CommandResult, ...]

    @property
    def final(self) -> CommandResult:
        """Return the CommandResult for the last stage."""
        return self.stages[-1]

    @property
    def ok(self) -> bool:
        """Return True when all pipeline stages exited successfully."""
        return all(stage.ok for stage in self.stages)

    @property
    def stdout(self) -> str | None:
        """Return the captured stdout from the last stage, when available."""
        return self.final.stdout


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

    def __or__(self, other: SafeCmd | Pipeline) -> Pipeline:
        """Compose this command with another stage, producing a Pipeline."""
        if isinstance(other, Pipeline):
            return Pipeline((self, *other.parts))
        return Pipeline((self, other))

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


@dc.dataclass(frozen=True, slots=True)
class Pipeline:
    """A sequence of SafeCmd stages connected via stdout/stdin piping."""

    parts: tuple[SafeCmd, ...]

    def __post_init__(self) -> None:
        """Validate stage count invariants."""
        if len(self.parts) < _MIN_PIPELINE_STAGES:
            msg = "Pipeline must contain at least two stages"
            raise ValueError(msg)

    def __or__(self, other: SafeCmd | Pipeline) -> Pipeline:
        """Compose pipelines, appending stages in left-to-right order."""
        if isinstance(other, Pipeline):
            return Pipeline((*self.parts, *other.parts))
        return Pipeline((*self.parts, other))

    async def run(
        self,
        *,
        capture: bool = True,
        echo: bool = False,
        context: ExecutionContext | None = None,
    ) -> PipelineResult:
        """Execute the pipeline asynchronously with streaming and backpressure."""
        return await _run_pipeline(
            self.parts,
            capture=capture,
            echo=echo,
            context=context,
        )

    def run_sync(
        self,
        *,
        capture: bool = True,
        echo: bool = False,
        context: ExecutionContext | None = None,
    ) -> PipelineResult:
        """Execute the pipeline synchronously via ``asyncio.run``."""
        return asyncio.run(self.run(capture=capture, echo=echo, context=context))


@dc.dataclass(frozen=True, slots=True)
class _PipelineRunConfig:
    ctx: ExecutionContext
    capture: bool
    echo: bool
    stdout_sink: typ.IO[str]
    stderr_sink: typ.IO[str]

    @property
    def capture_or_echo(self) -> bool:
        return self.capture or self.echo

    @property
    def stream_config(self) -> _StreamConfig:
        return _StreamConfig(
            capture_output=self.capture,
            echo_output=self.echo,
            sink=self.stdout_sink,
            encoding=self.ctx.encoding,
            errors=self.ctx.errors,
        )


@dc.dataclass(frozen=True, slots=True)
class _PipelineCompleted:
    processes: tuple[asyncio.subprocess.Process, ...]
    exit_codes: tuple[int, ...]
    stderr_by_stage: tuple[str | None, ...]
    final_stdout: str | None


def _prepare_pipeline_config(
    *,
    capture: bool,
    echo: bool,
    context: ExecutionContext | None,
) -> _PipelineRunConfig:
    """Normalise runtime options for pipeline execution."""
    ctx = context or ExecutionContext()
    stdout_sink = ctx.stdout_sink if ctx.stdout_sink is not None else sys.stdout
    stderr_sink = ctx.stderr_sink if ctx.stderr_sink is not None else sys.stderr
    return _PipelineRunConfig(
        ctx=ctx,
        capture=capture,
        echo=echo,
        stdout_sink=stdout_sink,
        stderr_sink=stderr_sink,
    )


async def _run_pipeline(
    parts: tuple[SafeCmd, ...],
    *,
    capture: bool,
    echo: bool,
    context: ExecutionContext | None,
) -> PipelineResult:
    """Execute a pipeline and return a structured result."""
    config = _prepare_pipeline_config(capture=capture, echo=echo, context=context)
    after_hooks_by_stage = tuple(_run_before_hooks(cmd) for cmd in parts)
    processes, stderr_tasks, stdout_task = await _spawn_pipeline_processes(
        parts,
        config,
    )
    pipe_tasks = _create_pipe_tasks(processes)
    exit_codes = await _wait_for_pipeline(
        processes,
        pipe_tasks=pipe_tasks,
        stream_tasks=_flatten_stream_tasks(stderr_tasks, stdout_task),
        cancel_grace=config.ctx.cancel_grace,
    )
    stderr_by_stage, final_stdout = await _collect_pipeline_streams(
        stderr_tasks,
        stdout_task,
    )
    stage_results = _build_pipeline_stage_results(
        parts,
        _PipelineCompleted(
            processes=tuple(processes),
            exit_codes=tuple(exit_codes),
            stderr_by_stage=tuple(stderr_by_stage),
            final_stdout=final_stdout,
        ),
    )
    _run_pipeline_after_hooks(parts, after_hooks_by_stage, stage_results)

    return PipelineResult(stages=tuple(stage_results))


async def _spawn_pipeline_processes(
    parts: tuple[SafeCmd, ...],
    config: _PipelineRunConfig,
) -> tuple[
    list[asyncio.subprocess.Process],
    list[asyncio.Task[str | None] | None],
    asyncio.Task[str | None] | None,
]:
    """Start subprocesses for each stage and wire up capture tasks."""
    processes: list[asyncio.subprocess.Process] = []
    stderr_tasks: list[asyncio.Task[str | None] | None] = []
    stdout_task: asyncio.Task[str | None] | None = None

    last_idx = len(parts) - 1
    for idx, cmd in enumerate(parts):
        stdin = asyncio.subprocess.DEVNULL if idx == 0 else asyncio.subprocess.PIPE
        stdout = (
            asyncio.subprocess.PIPE
            if idx != last_idx or config.capture_or_echo
            else asyncio.subprocess.DEVNULL
        )
        stderr = (
            asyncio.subprocess.PIPE
            if config.capture_or_echo
            else asyncio.subprocess.DEVNULL
        )

        process = await asyncio.create_subprocess_exec(
            *cmd.argv_with_program,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=_merge_env(config.ctx.env),
            cwd=str(config.ctx.cwd) if config.ctx.cwd is not None else None,
        )
        processes.append(process)

        stderr_tasks.append(
            asyncio.create_task(
                _consume_stream(
                    process.stderr,
                    dc.replace(config.stream_config, sink=config.stderr_sink),
                ),
            )
            if config.capture_or_echo
            else None
        )

        if idx == last_idx and config.capture_or_echo:
            stdout_task = asyncio.create_task(
                _consume_stream(
                    process.stdout,
                    config.stream_config,
                ),
            )

    return processes, stderr_tasks, stdout_task


def _create_pipe_tasks(
    processes: list[asyncio.subprocess.Process],
) -> list[asyncio.Task[None]]:
    """Create streaming tasks between adjacent pipeline stages."""
    return [
        asyncio.create_task(
            _pump_stream(
                processes[idx].stdout,
                processes[idx + 1].stdin,
            ),
        )
        for idx in range(len(processes) - 1)
    ]


def _flatten_stream_tasks(
    stderr_tasks: list[asyncio.Task[str | None] | None],
    stdout_task: asyncio.Task[str | None] | None,
) -> list[asyncio.Task[str | None]]:
    """Collect all running stream consumer tasks for cancellation cleanup."""
    tasks = [task for task in stderr_tasks if task is not None]
    if stdout_task is not None:
        tasks.append(stdout_task)
    return tasks


async def _wait_for_pipeline(
    processes: list[asyncio.subprocess.Process],
    *,
    pipe_tasks: list[asyncio.Task[None]],
    stream_tasks: list[asyncio.Task[str | None]],
    cancel_grace: float,
) -> list[int]:
    """Wait for pipeline completion, ensuring subprocess cleanup on cancellation."""
    try:
        return list(await asyncio.gather(*(p.wait() for p in processes)))
    except asyncio.CancelledError:
        await asyncio.gather(
            *(_terminate_process(p, cancel_grace) for p in processes),
            return_exceptions=True,
        )
        await asyncio.gather(*pipe_tasks, return_exceptions=True)
        await asyncio.gather(*stream_tasks, return_exceptions=True)
        raise
    finally:
        await asyncio.gather(*pipe_tasks, return_exceptions=True)


async def _collect_pipeline_streams(
    stderr_tasks: list[asyncio.Task[str | None] | None],
    stdout_task: asyncio.Task[str | None] | None,
) -> tuple[list[str | None], str | None]:
    """Collect captured stream output in stage order."""
    stderr_by_stage = [None if task is None else await task for task in stderr_tasks]

    final_stdout = None if stdout_task is None else await stdout_task
    return stderr_by_stage, final_stdout


def _build_pipeline_stage_results(
    parts: tuple[SafeCmd, ...],
    completed: _PipelineCompleted,
) -> list[CommandResult]:
    """Build a CommandResult for each pipeline stage."""
    last_idx = len(parts) - 1
    stage_results: list[CommandResult] = []
    for idx, cmd in enumerate(parts):
        stage_results.append(
            CommandResult(
                program=cmd.program,
                argv=cmd.argv,
                exit_code=completed.exit_codes[idx],
                pid=(
                    completed.processes[idx].pid
                    if completed.processes[idx].pid is not None
                    else -1
                ),
                stdout=completed.final_stdout if idx == last_idx else None,
                stderr=completed.stderr_by_stage[idx],
            ),
        )
    return stage_results


def _run_pipeline_after_hooks(
    parts: tuple[SafeCmd, ...],
    after_hooks_by_stage: tuple[tuple[AfterHook, ...], ...],
    results: list[CommandResult],
) -> None:
    """Run registered after hooks for each pipeline stage."""
    for cmd, hooks, result in zip(parts, after_hooks_by_stage, results, strict=True):
        for hook in hooks:
            hook(cmd, result)


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


async def _pump_stream(
    reader: asyncio.StreamReader | None,
    writer: asyncio.StreamWriter | None,
) -> None:
    """Stream stdout into stdin with backpressure via ``drain``.

    When the downstream stdin closes early (for example because the next stage
    terminates), this helper continues draining stdout to avoid deadlocking
    upstream stages.
    """
    if reader is None:
        return

    active_writer = writer
    while True:
        chunk = await reader.read(_READ_SIZE)
        if not chunk:
            break
        active_writer = await _write_to_stream_writer(active_writer, chunk)

    _close_stream_writer(active_writer)


async def _write_to_stream_writer(
    writer: asyncio.StreamWriter | None,
    chunk: bytes,
) -> asyncio.StreamWriter | None:
    """Write a chunk to a writer, returning None when downstream closes."""
    if writer is None:
        return None
    try:
        writer.write(chunk)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        return None
    return writer


def _close_stream_writer(writer: asyncio.StreamWriter | None) -> None:
    """Close a writer, swallowing errors from already-closed pipes."""
    if writer is None:
        return
    with contextlib.suppress(
        AttributeError,
        NotImplementedError,
        BrokenPipeError,
        ConnectionResetError,
    ):
        writer.write_eof()
    try:
        writer.close()
    except (BrokenPipeError, ConnectionResetError):
        return


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
    "Pipeline",
    "PipelineResult",
    "SafeCmd",
    "SafeCmdBuilder",
    "UnknownProgramError",
    "make",
]
