"""Safe command construction and execution facade for curated programs.

This module focuses on the typed core: building ``SafeCmd`` instances from
curated ``Program`` values and providing a minimal async runtime for executing
them with predictable semantics.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import dataclasses as dc
import typing as typ
import warnings
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
from cuprum._subprocess_execution import (
    _execute_subprocess,
    _resolve_timeout,
    _SubprocessExecution,
)
from cuprum.catalogue import (
    DEFAULT_CATALOGUE,
    ProgramCatalogue,
    ProjectSettings,
)
from cuprum.catalogue import UnknownProgramError as UnknownProgramError
from cuprum.context import current_context, merge_env_overlays
from cuprum.context import observe as observe
from cuprum.context import scoped as scoped

# Program must be imported at runtime rather than under TYPE_CHECKING
# because test modules instantiate CommandResult with Program values directly.
from cuprum.program import Program  # noqa: TC001

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
        """Return captured stdout, mirroring subprocess.TimeoutExpired.output."""
        return self.output


@dc.dataclass(frozen=True, slots=True)
class _ExecutionTracking:
    """Hook and task tracking for command execution."""

    execution_hooks: _ExecutionHooks
    pending_tasks: list[asyncio.Task[None]]


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

        Raises
        ------
        UnicodeEncodeError
            When *text* cannot be encoded using ``ctx.encoding`` and
            ``ctx.errors`` is ``"strict"`` (or another non-suppressing
            error handler).
        """
        if self.text is not None:
            return self.text.encode(ctx.encoding, ctx.errors)
        return self.data


@dc.dataclass(frozen=True, slots=True)
class RunOutputOptions:
    """Controls how a command's output streams are handled.

    Attributes
    ----------
    capture:
        When ``True`` capture stdout/stderr; otherwise discard them.
    echo:
        When ``True`` tee stdout/stderr to the parent process.
    """

    capture: bool = True
    echo: bool = False


@dc.dataclass(frozen=True, slots=True)
class IOOptions(RunOutputOptions):
    """Deprecated alias for command output stream options."""

    def __post_init__(self) -> None:
        """Emit a ``DeprecationWarning`` when ``IOOptions`` is constructed."""
        warnings.warn(
            "IOOptions is deprecated; use RunOutputOptions instead",
            DeprecationWarning,
            stacklevel=2,
        )


class _DeprecatedOutputFlags(typ.TypedDict, total=False):
    """Deprecated flat ``capture``/``echo`` flags for ``Pipeline.run``."""

    capture: bool
    echo: bool


def _resolve_pipeline_output(
    output: RunOutputOptions | None,
    flags: cabc.Mapping[str, bool],
) -> RunOutputOptions:
    """Resolve pipeline output options, deprecating flat ``capture``/``echo``.

    ``flags`` is typed loosely as a mapping because the type checker cannot
    yet propagate ``Unpack[_DeprecatedOutputFlags]`` kwargs; the callers keep
    the precise ``TypedDict`` surface. Mirrors the ``IOOptions`` deprecation
    pattern: the flat flags keep working but emit a ``DeprecationWarning``,
    and combining them with ``output`` is rejected because the caller's intent
    would be ambiguous. Unknown keys are rejected with ``TypeError`` to
    preserve the strict keyword surface.

    Example
    -------
    >>> _resolve_pipeline_output(None, {})
    RunOutputOptions(capture=True, echo=False)
    """
    unknown = set(flags) - {"capture", "echo"}
    if unknown:
        joined = ", ".join(sorted(unknown))
        msg = f"Pipeline.run/run_sync got unexpected keyword arguments: {joined}"
        raise TypeError(msg)
    if not flags:
        return output or RunOutputOptions()
    if output is not None:
        msg = "Pass either 'output' or the deprecated 'capture'/'echo' flags, not both"
        raise ValueError(msg)
    warnings.warn(
        "Pipeline.run/run_sync 'capture' and 'echo' keyword arguments are "
        "deprecated; pass output=RunOutputOptions(...) instead",
        DeprecationWarning,
        stacklevel=3,
    )
    return RunOutputOptions(
        capture=flags.get("capture", True),
        echo=flags.get("echo", False),
    )


def _prepare_execution_observation(
    cmd: SafeCmd,
    context: ExecutionContext,
    tracking: _ExecutionTracking,
    output: RunOutputOptions,
) -> _StageObservation:
    """Prepare the observation context for command execution."""
    cwd = Path(context.cwd) if context.cwd is not None else None
    scoped_overlay = current_context().env_overlay
    env_overlay = _freeze_str_mapping(
        merge_env_overlays(scoped_overlay, context.env),
    )
    tags = _merge_tags(
        {
            "project": cmd.project.name,
            "capture": output.capture,
            "echo": output.echo,
        },
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


async def _execute_with_hooks(
    cmd: SafeCmd,
    execution: _SubprocessExecution,
    tracking: _ExecutionTracking,
) -> CommandResult:
    """Execute *execution*, dispatch after-hooks, and handle cancellation."""
    try:
        result = await _execute_subprocess(execution)
        for hook in tracking.execution_hooks.after_hooks:
            hook(cmd, result)
    except asyncio.CancelledError:
        await asyncio.shield(_wait_for_exec_hook_tasks(tracking.pending_tasks))
        raise
    except BaseException:
        await _wait_for_exec_hook_tasks(tracking.pending_tasks)
        raise
    await _wait_for_exec_hook_tasks(tracking.pending_tasks)
    return result


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
        output: RunOutputOptions | None = None,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
        stdin: StdinInput | None = None,
    ) -> CommandResult:
        """Execute the command asynchronously with predictable cancellation.

        Parameters
        ----------
        output:
            Optional ``RunOutputOptions`` controlling stdout/stderr handling.
        timeout:
            Optional wall-clock timeout in seconds; ``None`` disables timeouts.
        context:
            Optional execution settings such as env, cwd, and cancel grace.
        stdin:
            Optional ``StdinInput`` data to feed to the subprocess.

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
        UnicodeEncodeError
            If ``stdin.text`` cannot be encoded with the active
            ``ExecutionContext`` encoding and errors settings.

        """
        out = output or RunOutputOptions()
        ctx = context or ExecutionContext()
        stdin_data = stdin.resolve(ctx) if stdin is not None else None
        effective_timeout = _resolve_timeout(timeout=timeout, context=context)
        tracking = _ExecutionTracking(
            execution_hooks=_run_before_hooks(self),
            pending_tasks=[],
        )
        observation = _prepare_execution_observation(
            self,
            ctx,
            tracking,
            out,
        )

        observation.emit("plan", _EventDetails(pid=None))
        for hook in tracking.execution_hooks.before_hooks:
            hook(self)

        return await _execute_with_hooks(
            self,
            _SubprocessExecution(
                cmd=self,
                ctx=ctx,
                capture=out.capture,
                echo=out.echo,
                timeout=effective_timeout,
                observation=observation,
                stdin_data=stdin_data,
            ),
            tracking,
        )

    def run_sync(
        self,
        *,
        output: RunOutputOptions | None = None,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
        stdin: StdinInput | None = None,
    ) -> CommandResult:
        """Execute the command synchronously.

        Mirrors :meth:`run`; all parameters and return semantics are identical.

        Raises
        ------
        ForbiddenProgramError
            If the program is not in the current context's allowlist.
        TimeoutExpired
            If the command exceeds the configured timeout.
        UnicodeEncodeError
            If ``stdin.text`` cannot be encoded with the active
            ``ExecutionContext`` encoding and errors settings.
        """
        return asyncio.run(
            self.run(output=output, timeout=timeout, context=context, stdin=stdin),
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
        output: RunOutputOptions | None = None,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
        **deprecated_flags: typ.Unpack[_DeprecatedOutputFlags],
    ) -> PipelineResult:
        """Execute the pipeline asynchronously with streaming and backpressure.

        Parameters
        ----------
        output:
            Optional ``RunOutputOptions`` controlling stdout/stderr handling,
            mirroring :meth:`SafeCmd.run`. Defaults to ``RunOutputOptions()``
            (capture on, echo off).
        timeout:
            Optional wall-clock timeout in seconds; ``None`` disables timeouts.
        context:
            Optional execution settings such as env, cwd, and cancel grace.
        deprecated_flags:
            Deprecated flat ``capture`` / ``echo`` flags retained for
            backwards compatibility; pass ``output=RunOutputOptions(...)``
            instead. Supplying either emits a ``DeprecationWarning``;
            combining them with ``output`` raises ``ValueError``.

        Returns
        -------
        PipelineResult
            Structured per-stage results for the completed pipeline.

        Raises
        ------
        ValueError
            If both ``output`` and the deprecated ``capture``/``echo`` flags
            are supplied.
        """
        out = _resolve_pipeline_output(output, deprecated_flags)
        effective_timeout = _resolve_timeout(timeout=timeout, context=context)
        config = _prepare_pipeline_config(
            capture=out.capture,
            echo=out.echo,
            timeout=effective_timeout,
            context=context,
        )
        return await _run_pipeline(self.parts, config)

    def run_sync(
        self,
        *,
        output: RunOutputOptions | None = None,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
        **deprecated_flags: typ.Unpack[_DeprecatedOutputFlags],
    ) -> PipelineResult:
        """Execute the pipeline synchronously via ``asyncio.run``.

        Mirrors :meth:`run`; all parameters and return semantics are identical,
        including the deprecation of the flat ``capture``/``echo`` flags.
        """
        # Resolve here so the DeprecationWarning points at the caller rather
        # than at the internal ``self.run`` delegation.
        out = _resolve_pipeline_output(output, deprecated_flags)
        return asyncio.run(
            self.run(
                output=out,
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
        """Coerce ``args``/``kwargs`` into a ``SafeCmd`` for the program."""
        argv = _coerce_argv(args, kwargs)
        return SafeCmd(program=entry.program, argv=argv, project=entry.project)

    return builder


__all__ = [
    "CommandResult",
    "ExecutionContext",
    "IOOptions",
    "Pipeline",
    "PipelineResult",
    "RunOutputOptions",
    "SafeCmd",
    "SafeCmdBuilder",
    "StdinInput",
    "TimeoutExpired",
    "UnknownProgramError",
    "make",
    "observe",
    "scoped",
]
