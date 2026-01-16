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
    _ExecutionHooks,
    _run_before_hooks,
    _run_pipeline,
    _StageObservation,
)
from cuprum._pipeline_streams import _prepare_pipeline_config
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
from cuprum.context import current_context as _current_context
from cuprum.context import observe as observe
from cuprum.context import scoped as scoped

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
    timeout:
        Optional runtime timeout in seconds. ``None`` means no override.
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
    timeout: float | None = None
    stdout_sink: typ.IO[str] | None = None
    stderr_sink: typ.IO[str] | None = None
    encoding: str = _DEFAULT_ENCODING
    errors: str = _DEFAULT_ERROR_HANDLING
    tags: cabc.Mapping[str, object] | None = None


class TimeoutExpired(TimeoutError):  # noqa: N818  # match subprocess.TimeoutExpired naming.
    """Raised when command execution exceeds the configured timeout."""

    def __init__(
        self,
        *,
        cmd: typ.Sequence[str] | object,
        timeout: float,
        output: str | bytes | None = None,
        stderr: str | bytes | None = None,
    ) -> None:
        super().__init__(f"Command {cmd!r} timed out after {timeout} seconds")
        self.cmd = cmd
        self.timeout = timeout
        self.output = output
        self.stderr = stderr

    @property
    def stdout(self) -> str | bytes | None:
        """Return captured stdout, mirroring subprocess.TimeoutExpired.output."""
        return self.output


def _resolve_timeout(
    *,
    timeout: float | None,
    context: ExecutionContext | None,
) -> float | None:
    """Resolve the effective timeout from explicit, context, and scoped values."""
    if timeout is not None:
        return timeout
    if context is not None and context.timeout is not None:
        return context.timeout
    return _current_context().timeout


async def _wait_for_exit_code(
    process: asyncio.subprocess.Process,
    ctx: ExecutionContext,
    *,
    timeout: float | None = None,
    consumers: tuple[asyncio.Task[typ.Any], ...] = (),
) -> tuple[int, float]:
    """Wait for a subprocess, handling cancellation and capturing exit time."""
    try:
        if timeout is None:
            exit_code = await process.wait()
        else:
            exit_code = await asyncio.wait_for(process.wait(), timeout)
    except TimeoutError:
        await _terminate_process(process, ctx.cancel_grace)
        if consumers:
            await asyncio.gather(*consumers, return_exceptions=True)
        raise
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
    timeout: float | None
    observation: _StageObservation


class _SubprocessTimeoutError(Exception):
    """Internal wrapper for subprocess timeouts with captured output."""

    def __init__(
        self,
        *,
        timeout: float,
        stdout: str | None,
        stderr: str | None,
        exited_at: float,
    ) -> None:
        super().__init__(f"Execution exceeded {timeout}s timeout")
        self.timeout = timeout
        self.stdout = stdout
        self.stderr = stderr
        self.exited_at = exited_at


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


def _create_stream_callback(
    observation: _StageObservation,
    event_type: typ.Literal["stdout", "stderr"],
    pid: int | None,
) -> cabc.Callable[[str], None] | None:
    """Create a callback for emitting stream line events, or None if no hooks."""
    if not observation.hooks.observe_hooks:
        return None
    return lambda line: observation.emit(event_type, _EventDetails(pid=pid, line=line))


def _spawn_stream_consumers(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    stream_config: _StreamConfig,
    *,
    pid: int | None,
) -> tuple[asyncio.Task[str | None], asyncio.Task[str | None]]:
    """Spawn stdout and stderr stream consumer tasks."""
    stdout_on_line = _create_stream_callback(execution.observation, "stdout", pid)
    stderr_on_line = _create_stream_callback(execution.observation, "stderr", pid)
    stderr_config = dc.replace(
        stream_config,
        sink=(
            execution.ctx.stderr_sink
            if execution.ctx.stderr_sink is not None
            else sys.stderr
        ),
    )
    return (
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
                stderr_config,
                on_line=stderr_on_line,
            ),
        ),
    )


async def _run_subprocess_with_streams(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    *,
    pid: int | None,
    timeout: float | None,
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
    consumers = _spawn_stream_consumers(process, execution, stream_config, pid=pid)
    try:
        exit_code, exited_at = await _wait_for_exit_code(
            process,
            execution.ctx,
            timeout=timeout,
            consumers=consumers,
        )
    except TimeoutError as exc:
        stdout_text, stderr_text = await asyncio.gather(*consumers)
        if timeout is None:
            msg = "TimeoutError without a configured timeout"
            raise RuntimeError(msg) from exc
        raise _SubprocessTimeoutError(
            timeout=timeout,
            stdout=stdout_text,
            stderr=stderr_text,
            exited_at=time.perf_counter(),
        ) from exc
    stdout_text, stderr_text = await asyncio.gather(*consumers)
    return exit_code, exited_at, stdout_text, stderr_text


def _get_exit_code(process: asyncio.subprocess.Process) -> int:
    """Return the process exit code, defaulting to -1 if unavailable."""
    return process.returncode if process.returncode is not None else -1


def _emit_exit_event(  # noqa: PLR0913
    observation: _StageObservation,
    *,
    pid: int | None,
    exit_code: int,
    started_at: float,
    exited_at: float,
) -> None:
    """Emit an exit event with process details and duration."""
    observation.emit(
        "exit",
        _EventDetails(
            pid=pid,
            exit_code=exit_code,
            duration_s=max(0.0, exited_at - started_at),
        ),
    )


@dc.dataclass(frozen=True, slots=True)
class _TimeoutContext:
    """Context information for a timeout exception."""

    cmd_argv: tuple[str, ...]
    timeout: float
    stdout: str | None
    stderr: str | None


@dc.dataclass(frozen=True, slots=True)
class _ExecutionTracking:
    """Hook and task tracking for command execution."""

    execution_hooks: _ExecutionHooks
    pending_tasks: list[asyncio.Task[None]]


def _raise_timeout_expired(
    timeout_ctx: _TimeoutContext,
    exc: BaseException,
) -> typ.NoReturn:
    """Raise TimeoutExpired with captured output and chain the original exception."""
    raise TimeoutExpired(
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
        timeout = ctx.execution.timeout
        if timeout is None:
            msg = "TimeoutError without a configured timeout"
            raise RuntimeError(msg) from exc
        exited_at = time.perf_counter()
        stdout = ctx.stdout_text
        stderr = ctx.stderr_text

    exit_code = _get_exit_code(ctx.process)
    _emit_exit_event(
        ctx.execution.observation,
        pid=ctx.process.pid,
        exit_code=exit_code,
        started_at=ctx.started_at,
        exited_at=exited_at,
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


async def _execute_subprocess(execution: _SubprocessExecution) -> CommandResult:
    process = await _spawn_subprocess(execution)
    started_at = time.perf_counter()
    pid = process.pid
    execution.observation.emit("start", _EventDetails(pid=pid))

    stdout_text: str | None = None
    stderr_text: str | None = None
    try:
        if not execution.capture and not execution.echo:
            exit_code, exited_at = await _wait_for_exit_code(
                process,
                execution.ctx,
                timeout=execution.timeout,
            )
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
                timeout=execution.timeout,
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

    _emit_exit_event(
        execution.observation,
        pid=pid,
        exit_code=exit_code,
        started_at=started_at,
        exited_at=exited_at,
    )

    return CommandResult(
        program=execution.cmd.program,
        argv=execution.cmd.argv,
        exit_code=exit_code,
        pid=process.pid if process.pid is not None else -1,
        stdout=stdout_text,
        stderr=stderr_text,
    )


def _prepare_execution_observation(  # noqa: PLR0913
    cmd: SafeCmd,
    context: ExecutionContext,
    tracking: _ExecutionTracking,
    *,
    capture: bool,
    echo: bool,
) -> _StageObservation:
    """Prepare the observation context for command execution."""
    cwd = Path(context.cwd) if context.cwd is not None else None
    env_overlay = _freeze_str_mapping(context.env)
    tags = _merge_tags(
        {"project": cmd.project.name, "capture": capture, "echo": echo},
        context.tags,
    )
    return _StageObservation(
        cmd=cmd,
        hooks=tracking.execution_hooks,
        cwd=cwd,
        env_overlay=env_overlay,
        tags=tags,
        pending_tasks=tracking.pending_tasks,
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
        timeout: float | None = None,
        context: ExecutionContext | None = None,
    ) -> CommandResult:
        """Execute the command asynchronously with predictable cancellation.

        Parameters
        ----------
        capture:
            When ``True`` capture stdout/stderr; otherwise discard them.
        echo:
            When ``True`` tee stdout/stderr to the parent process.
        timeout:
            Optional wall-clock timeout in seconds; ``None`` disables timeouts.
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
        TimeoutExpired
            If the command exceeds the configured timeout.

        """
        ctx = context or ExecutionContext()
        effective_timeout = _resolve_timeout(timeout=timeout, context=context)
        tracking = _ExecutionTracking(
            execution_hooks=_run_before_hooks(self),
            pending_tasks=[],
        )
        observation = _prepare_execution_observation(
            self,
            ctx,
            tracking,
            capture=capture,
            echo=echo,
        )

        observation.emit("plan", _EventDetails(pid=None))
        for hook in tracking.execution_hooks.before_hooks:
            hook(self)

        try:
            result = await _execute_subprocess(
                _SubprocessExecution(
                    cmd=self,
                    ctx=ctx,
                    capture=capture,
                    echo=echo,
                    timeout=effective_timeout,
                    observation=observation,
                ),
            )
            for hook in tracking.execution_hooks.after_hooks:
                hook(self, result)
        except asyncio.CancelledError:
            await asyncio.shield(_wait_for_exec_hook_tasks(tracking.pending_tasks))
            raise
        except BaseException:
            await _wait_for_exec_hook_tasks(tracking.pending_tasks)
            raise

        await _wait_for_exec_hook_tasks(tracking.pending_tasks)
        return result

    def run_sync(
        self,
        *,
        capture: bool = True,
        echo: bool = False,
        timeout: float | None = None,
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
        timeout:
            Optional wall-clock timeout in seconds; ``None`` disables timeouts.
        context:
            Optional execution settings such as env, cwd, and cancel grace.

        Returns
        -------
        CommandResult
            Structured information about the completed process.

        """
        return asyncio.run(
            self.run(
                capture=capture,
                echo=echo,
                timeout=timeout,
                context=context,
            ),
        )


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
        timeout: float | None = None,
        context: ExecutionContext | None = None,
    ) -> PipelineResult:
        """Execute the pipeline asynchronously with streaming and backpressure."""
        effective_timeout = _resolve_timeout(timeout=timeout, context=context)
        config = _prepare_pipeline_config(
            capture=capture,
            echo=echo,
            timeout=effective_timeout,
            context=context,
        )
        return await _run_pipeline(self.parts, config)

    def run_sync(
        self,
        *,
        capture: bool = True,
        echo: bool = False,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
    ) -> PipelineResult:
        """Execute the pipeline synchronously via ``asyncio.run``."""
        return asyncio.run(
            self.run(
                capture=capture,
                echo=echo,
                timeout=timeout,
                context=context,
            ),
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


__all__ = [
    "CommandResult",
    "ExecutionContext",
    "Pipeline",
    "PipelineResult",
    "SafeCmd",
    "SafeCmdBuilder",
    "TimeoutExpired",
    "UnknownProgramError",
    "make",
    "observe",
    "scoped",
]
