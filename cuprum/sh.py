"""Safe command construction and execution facade for curated programs.

This module focuses on the typed core: building ``SafeCmd`` instances from
curated ``Program`` values and providing a minimal async runtime for executing
them with predictable semantics.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import dataclasses as dc
import time
import typing as typ
import warnings
from pathlib import Path

from cuprum._observability import (
    _base_stage_tags,
    _drain_tasks_during_cleanup,
    _merge_tags,
    _resolve_env_overlay,
    _wait_for_exec_hook_tasks,
)
from cuprum._pipeline_config import _prepare_pipeline_config
from cuprum._pipeline_internals import (
    _MIN_PIPELINE_STAGES,
    _collect_hooks,
    _enforce_allowlist,
    _EventDetails,
    _ExecutionHooks,
    _run_pipeline,
    _StageObservation,
)
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._subprocess_context import _resolve_timeout
from cuprum._subprocess_execution import (
    _execute_subprocess,
    _SubprocessExecution,
)
from cuprum.catalogue import (
    DEFAULT_CATALOGUE,
    ProgramCatalogue,
    ProjectSettings,
)
from cuprum.catalogue import UnknownProgramError as UnknownProgramError
from cuprum.context import _validate_timeout
from cuprum.context import current_context as current_context
from cuprum.context import observe as observe
from cuprum.context import scoped as scoped

# Public annotations use ``Program``. Keep it in module globals so
# ``typing.get_type_hints`` can resolve the postponed public annotations.
from cuprum.program import (
    Program,  # ruff: ignore[typing-only-first-party-import] - public annotations must resolve at runtime
)

type _ArgValue = str | int | float | bool | Path
type SafeCmdBuilder = cabc.Callable[..., SafeCmd]
type _EnvMapping = cabc.Mapping[str, str] | None
type _CwdType = str | Path | None

_DEFAULT_CANCEL_GRACE = 0.5
_DEFAULT_NATIVE_PUMP_CLEANUP_GRACE = 0.5
# Names the aggregate raised when draining observe-hook tasks fails while a
# single-command execution is already unwinding.
_COMMAND_FINALIZATION_ERROR = "command finalization failed"
_DEFAULT_ENCODING = "utf-8"
_DEFAULT_ERROR_HANDLING = "replace"


def _stringify_arg(value: _ArgValue) -> str:
    """Convert values into argv-safe strings."""
    if value is None:
        # None is disallowed because it is almost always a mistake in CLI argv
        # construction; callers must represent missing values themselves (for
        # example, by omitting the flag) before invoking sh.make.
        msg = "None is not a valid argv element for sh.make"
        raise TypeError(msg)
    return str(value)


def _serialize_kwargs(kwargs: dict[str, _ArgValue]) -> tuple[str, ...]:
    """Serialize keyword arguments to CLI-style ``--flag=value`` entries."""
    flags: list[str] = []
    for key, value in kwargs.items():
        normalized_key = key.replace("_", "-")
        flags.append(f"--{normalized_key}={_stringify_arg(value)}")
    return tuple(flags)


def build_argv(*args: _ArgValue, **kwargs: _ArgValue) -> tuple[str, ...]:
    """Build an argv tuple using the same rules as ``sh.make`` builders.

    Parameters
    ----------
    *args
        Positional argument values. Values are stringified with ``str()`` in
        the order supplied and appear before generated keyword flags.
    **kwargs
        Keyword flag values. Each key is normalized by replacing underscores
        with hyphens, then serialized as ``--flag=value`` in insertion order.
        ``None`` is rejected in positional and keyword positions.

    Returns
    -------
    tuple[str, ...]
        The constructed argv tuple, excluding the program name.

    Examples
    --------
    >>> build_argv("status", porcelain=True, branch="main")
    ('status', '--porcelain=True', '--branch=main')
    """
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
        """Whether the command exited successfully.

        Returns
        -------
        bool
            ``True`` exactly when ``exit_code`` is zero.
        """
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
        """The result from the final pipeline stage.

        Returns
        -------
        CommandResult
            The last stage's result in execution order.
        """
        return self.stages[-1]

    @property
    def failure(self) -> CommandResult | None:
        """The stage that triggered fail-fast termination, if any.

        Returns
        -------
        CommandResult | None
            The failing stage result, or ``None`` when no stage triggered
            fail-fast termination.
        """
        if self.failure_index is None:
            return None
        return self.stages[self.failure_index]

    @property
    def ok(self) -> bool:
        """Whether every pipeline stage exited successfully.

        Returns
        -------
        bool
            ``True`` when every stage result is successful; otherwise
            ``False``.
        """
        return all(stage.ok for stage in self.stages)

    @property
    def stdout(self) -> str | None:
        """Captured output from the final pipeline stage.

        Returns
        -------
        str | None
            The final stage's captured standard output, or ``None`` when
            capture was disabled.
        """
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
    native_pump_cleanup_grace:
        Seconds to wait for a cancelled native-pump worker before its
        descriptor cleanup is deferred to its completion callback.
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
    flags: _DeprecatedOutputFlags,
) -> RunOutputOptions:
    """Resolve pipeline output options, deprecating flat ``capture``/``echo``."""
    # Callers forward their ``Unpack[_DeprecatedOutputFlags]`` kwargs verbatim,
    # so the parameter keeps the precise ``TypedDict`` surface. Unknown keys
    # can still arrive at runtime (a ``TypedDict`` is open), and are rejected
    # here to preserve the strict keyword surface.
    unknown = set(flags) - {"capture", "echo"}
    if unknown:
        joined = ", ".join(sorted(unknown))
        msg = f"Pipeline.run/run_sync got unexpected keyword arguments: {joined}"
        raise TypeError(msg)
    if not flags:
        return output or RunOutputOptions()
    if output is not None:
        # Reject combining the deprecated flat flags with ``output``: the
        # caller's intent would otherwise be ambiguous.
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
    env_overlay = _resolve_env_overlay(context.env)
    tags = _merge_tags(
        _base_stage_tags(
            cmd,
            capture=output.capture,
            echo=output.echo,
        ),
        context.tags,
    )
    return _StageObservation(
        cmd=cmd,
        hooks=tracking.execution_hooks,
        cwd=cwd,
        env_overlay=env_overlay,
        tags=tags,
        pending_tasks=tracking.pending_tasks,
        wall_clock=time.time,
    )


async def _execute_with_hooks(
    cmd: SafeCmd,
    execution: _SubprocessExecution,
    tracking: _ExecutionTracking,
) -> CommandResult:
    """Execute *execution*, dispatch after-hooks, and handle cancellation.

    Draining the observe-hook tasks during cleanup must not let a failing
    background hook stand in for the error that triggered the cleanup: a caller
    awaiting ``TimeoutExpired`` (or a cancellation) would otherwise see the
    hook's exception instead. Both cleanup paths therefore drain through
    :func:`_drain_tasks_during_cleanup`, which aggregates a drain failure with
    the active error into a ``BaseExceptionGroup`` rather than replacing it —
    matching the pipeline path. The drain on the success path still surfaces a
    hook failure directly, because there is no primary error to preserve.

    Every drain runs through :func:`_shielded_cleanup` rather than a bare
    ``await asyncio.shield(...)``. The shield alone keeps the cancellation off
    the drain, but the *awaiting* coroutine resumes immediately, so the run
    would propagate its ``CancelledError`` while the hook tasks were still
    settling — leaking exactly the tasks the drain exists to reconcile.

    Returns
    -------
    CommandResult
        The completed command's result, once every after-hook has run and the
        observe-hook tasks have drained.
    """
    try:
        result = await _execute_subprocess(execution)
        for hook in tracking.execution_hooks.after_hooks:
            hook(cmd, result)
    except BaseException as run_error:
        await _shielded_cleanup(
            _drain_tasks_during_cleanup(
                tracking.pending_tasks,
                run_error,
                message=_COMMAND_FINALIZATION_ERROR,
            )
        )
        raise
    await _shielded_cleanup(_wait_for_exec_hook_tasks(tracking.pending_tasks))
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
        """The program name followed by this command's arguments.

        Returns
        -------
        tuple[str, ...]
            An argument vector whose first item is ``str(program)``.
        """
        return (str(self.program), *self.argv)

    def __or__(self, other: SafeCmd | Pipeline) -> Pipeline:
        """Compose this command with another stage, producing a Pipeline."""
        return Pipeline.concat(self, other)

    async def run(
        self,
        *,
        output: RunOutputOptions | None = None,
        # ASYNC109: `timeout` is public API mirroring subprocess.run(timeout=…),
        # not a callee-owned deadline; keeping it is a deliberate design choice.
        timeout: float | None = None,  # ruff: ignore[async-function-with-timeout]
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
            If the program is not permitted by the active context allowlist.
        TimeoutExpired
            If *timeout* elapses before the command completes.
        UnicodeEncodeError
            If ``stdin`` text cannot be encoded with the context's encoding.
        """  # ruff: ignore[docstring-extraneous-exception] - all propagate from allowlist, timeout, and stdin encode
        out = output or RunOutputOptions()
        ctx = context or ExecutionContext()
        _enforce_allowlist(self)
        stdin_data = stdin.resolve(ctx) if stdin is not None else None
        effective_timeout = _resolve_timeout(timeout=timeout, context=context)
        tracking = _ExecutionTracking(
            execution_hooks=_collect_hooks(current_context()),
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

        Returns
        -------
        CommandResult
            Structured information about the completed process.

        Raises
        ------
        ForbiddenProgramError
            If the program is not permitted by the active context allowlist.
        TimeoutExpired
            If *timeout* elapses before the command completes.
        UnicodeEncodeError
            If ``stdin`` text cannot be encoded with the context's encoding.
        """  # ruff: ignore[docstring-extraneous-exception] - all propagate from allowlist, timeout, and stdin encode
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
        """Compose a pipeline from two stage operands.

        Parameters
        ----------
        left : SafeCmd | Pipeline
            A command or pipeline whose stages come first.
        right : SafeCmd | Pipeline
            A command or pipeline whose stages follow ``left``'s.

        Returns
        -------
        Pipeline
            A pipeline whose stages are *left*'s followed by *right*'s.
        """
        left_parts = left.parts if isinstance(left, Pipeline) else (left,)
        right_parts = right.parts if isinstance(right, Pipeline) else (right,)
        return cls((*left_parts, *right_parts))

    async def run(
        self,
        *,
        output: RunOutputOptions | None = None,
        # ASYNC109: `timeout` is public API mirroring subprocess.run(timeout=…),
        # not a callee-owned deadline; keeping it is a deliberate design choice.
        timeout: float | None = None,  # ruff: ignore[async-function-with-timeout]
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
        ForbiddenProgramError
            If any stage's program is not permitted by the active context
            allowlist.
        TimeoutExpired
            If *timeout* elapses before the pipeline completes.
        TypeError
            If ``deprecated_flags`` contains keys other than ``capture`` or
            ``echo``.
        ValueError
            If ``output`` is combined with the deprecated ``capture``/``echo``
            flags.
        """  # ruff: ignore[docstring-extraneous-exception] - all propagate from allowlist, timeout, and output resolver
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

        Returns
        -------
        PipelineResult
            Structured per-stage results for the completed pipeline.

        Raises
        ------
        ForbiddenProgramError
            If any stage's program is not permitted by the active context
            allowlist.
        TimeoutExpired
            If *timeout* elapses before the pipeline completes.
        TypeError
            If an unexpected deprecated output keyword argument is supplied.
        ValueError
            If ``output`` is combined with the deprecated ``capture``/``echo``
            flags.
        """  # ruff: ignore[docstring-extraneous-exception] - all propagate from allowlist, timeout, and output resolver
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

    Parameters
    ----------
    program : Program
        The program the built ``SafeCmd`` instances invoke; it must exist in
        ``catalogue``.
    catalogue : ProgramCatalogue
        The catalogue used to validate ``program`` and resolve its entry.

    Returns
    -------
    SafeCmdBuilder
        A callable that builds ``SafeCmd`` instances for ``program``.

    Raises
    ------
    UnknownProgramError
        If ``program`` does not exist in ``catalogue``.
    """  # ruff: ignore[docstring-extraneous-exception] - UnknownProgramError propagates from catalogue.lookup
    entry = catalogue.lookup(program)

    def builder(*args: _ArgValue, **kwargs: _ArgValue) -> SafeCmd:
        """Coerce ``args``/``kwargs`` into a ``SafeCmd`` for the program."""
        argv = build_argv(*args, **kwargs)
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
    "build_argv",
    "make",
    "observe",
    "scoped",
]
