"""Safe command construction and execution facade for curated programs.

This module focuses on the typed core: building ``SafeCmd`` instances from
curated ``Program`` values and providing a minimal async runtime for executing
them with predictable semantics.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import dataclasses as dc
import sys
import time
import typing as typ
from pathlib import Path

from cuprum._observability import (
    _freeze_str_mapping,
    _merge_tags,
    _wait_for_exec_hook_tasks,
)
from cuprum._pipeline_internals import (
    _MIN_PIPELINE_STAGES,
    _EventDetails,
    _run_before_hooks,
    _run_pipeline,
    _StageObservation,
)
from cuprum._process_lifecycle import _merge_env, _terminate_process
from cuprum._streams import (
    _consume_stream,
    _StreamConfig,
)
from cuprum.catalogue import (
    DEFAULT_CATALOGUE,
    ProgramCatalogue,
    ProjectSettings,
)
from cuprum.catalogue import UnknownProgramError as UnknownProgramError
from cuprum.context import observe as observe

if typ.TYPE_CHECKING:
    from cuprum.program import Program

type _ArgValue = str | int | float | bool | Path
type SafeCmdBuilder = cabc.Callable[..., SafeCmd]
type _EnvMapping = cabc.Mapping[str, str] | None
type _CwdType = str | Path | None

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
class PipelineResult:
    """Structured result returned by pipeline execution.

    Attributes
    ----------
    stages:
        Command results for each pipeline stage, in execution order. For stages
        whose stdout is streamed into the next stage, ``stdout`` is ``None``.
        The final stage carries captured stdout when enabled.
    failure_index:
        Index of the stage that triggered fail-fast termination, or ``None``
        when all stages completed successfully.

    """

    stages: tuple[CommandResult, ...]
    failure_index: int | None = None

    @property
    def final(self) -> CommandResult:
        """Return the CommandResult for the last stage."""
        return self.stages[-1]

    @property
    def failure(self) -> CommandResult | None:
        """Return the stage that triggered fail-fast termination, when any."""
        if self.failure_index is None:
            return None
        return self.stages[self.failure_index]

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
    tags:
        Optional metadata attached to structured execution events.

    """

    env: _EnvMapping = None
    cwd: _CwdType = None
    cancel_grace: float = _DEFAULT_CANCEL_GRACE
    stdout_sink: typ.IO[str] | None = None
    stderr_sink: typ.IO[str] | None = None
    encoding: str = _DEFAULT_ENCODING
    errors: str = _DEFAULT_ERROR_HANDLING
    tags: cabc.Mapping[str, object] | None = None


async def _wait_for_exit_code(
    process: asyncio.subprocess.Process,
    ctx: ExecutionContext,
    *,
    consumers: tuple[asyncio.Task[typ.Any], ...] = (),
) -> tuple[int, float]:
    """Wait for a subprocess, handling cancellation and capturing exit time."""
    try:
        exit_code = await process.wait()
    except asyncio.CancelledError:
        await _terminate_process(process, ctx.cancel_grace)
        if consumers:
            await asyncio.gather(*consumers, return_exceptions=True)
        raise
    else:
        exited_at = time.perf_counter()
        return exit_code, exited_at


@dc.dataclass(frozen=True, slots=True)
class _SubprocessExecution:
    cmd: SafeCmd
    ctx: ExecutionContext
    capture: bool
    echo: bool
    observation: _StageObservation


async def _spawn_subprocess(
    execution: _SubprocessExecution,
) -> asyncio.subprocess.Process:
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
        env=_merge_env(execution.ctx.env),
        cwd=(str(execution.ctx.cwd) if execution.ctx.cwd is not None else None),
    )


async def _run_subprocess_with_streams(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    *,
    pid: int | None,
) -> tuple[int, float, str | None, str | None]:
    stream_config = _StreamConfig(
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

    stdout_on_line = (
        None
        if not execution.observation.hooks.observe_hooks
        else lambda line: execution.observation.emit(
            "stdout",
            _EventDetails(pid=pid, line=line),
        )
    )
    stderr_on_line = (
        None
        if not execution.observation.hooks.observe_hooks
        else lambda line: execution.observation.emit(
            "stderr",
            _EventDetails(pid=pid, line=line),
        )
    )
    consumers = (
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
                dc.replace(
                    stream_config,
                    sink=(
                        execution.ctx.stderr_sink
                        if execution.ctx.stderr_sink is not None
                        else sys.stderr
                    ),
                ),
                on_line=stderr_on_line,
            ),
        ),
    )
    exit_code, exited_at = await _wait_for_exit_code(
        process,
        execution.ctx,
        consumers=consumers,
    )
    stdout_text, stderr_text = await asyncio.gather(*consumers)
    return exit_code, exited_at, stdout_text, stderr_text


async def _execute_subprocess(execution: _SubprocessExecution) -> CommandResult:
    process = await _spawn_subprocess(execution)
    started_at = time.perf_counter()
    pid = process.pid
    execution.observation.emit("start", _EventDetails(pid=pid))

    stdout_text: str | None = None
    stderr_text: str | None = None
    if not execution.capture and not execution.echo:
        exit_code, exited_at = await _wait_for_exit_code(process, execution.ctx)
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

    execution.observation.emit(
        "exit",
        _EventDetails(
            pid=pid,
            exit_code=exit_code,
            duration_s=max(0.0, exited_at - started_at),
        ),
    )

    return CommandResult(
        program=execution.cmd.program,
        argv=execution.cmd.argv,
        exit_code=exit_code,
        pid=process.pid if process.pid is not None else -1,
        stdout=stdout_text,
        stderr=stderr_text,
    )


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
        return Pipeline.concat(self, other)

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
        ctx = context or ExecutionContext()
        execution_hooks = _run_before_hooks(self)
        pending_tasks: list[asyncio.Task[None]] = []
        cwd = Path(ctx.cwd) if ctx.cwd is not None else None
        env_overlay = _freeze_str_mapping(ctx.env)
        tags = _merge_tags(
            {"project": self.project.name, "capture": capture, "echo": echo},
            ctx.tags,
        )
        observation = _StageObservation(
            cmd=self,
            hooks=execution_hooks,
            cwd=cwd,
            env_overlay=env_overlay,
            tags=tags,
            pending_tasks=pending_tasks,
        )

        observation.emit("plan", _EventDetails(pid=None))
        for hook in execution_hooks.before_hooks:
            hook(self)

        try:
            result = await _execute_subprocess(
                _SubprocessExecution(
                    cmd=self,
                    ctx=ctx,
                    capture=capture,
                    echo=echo,
                    observation=observation,
                ),
            )
            for hook in execution_hooks.after_hooks:
                hook(self, result)
        except asyncio.CancelledError:
            await asyncio.shield(_wait_for_exec_hook_tasks(pending_tasks))
            raise
        except BaseException:
            await _wait_for_exec_hook_tasks(pending_tasks)
            raise

        await _wait_for_exec_hook_tasks(pending_tasks)
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
        return Pipeline.concat(self, other)

    @classmethod
    def concat(cls, left: SafeCmd | Pipeline, right: SafeCmd | Pipeline) -> Pipeline:
        """Compose a pipeline from two pipeline operands."""
        left_parts = left.parts if isinstance(left, Pipeline) else (left,)
        right_parts = right.parts if isinstance(right, Pipeline) else (right,)
        return cls((*left_parts, *right_parts))

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


__all__ = [
    "CommandResult",
    "ExecutionContext",
    "Pipeline",
    "PipelineResult",
    "SafeCmd",
    "SafeCmdBuilder",
    "UnknownProgramError",
    "make",
    "observe",
]
