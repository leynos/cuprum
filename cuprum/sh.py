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

from cuprum._constants import DEFAULT_ECHO_MAX_LINE_BYTES
from cuprum._idle_diagnostic import _idle_subject
from cuprum._idle_heartbeat import _build_idle_monitor, _validate_idle_options
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
from cuprum.echo_events import (
    RelayFallback as RelayFallback,  # ruff: ignore[typing-only-first-party-import] - public annotations must resolve at runtime,
)

# Public annotations use ``Program``. Keep it in module globals so
# ``typing.get_type_hints`` can resolve the postponed public annotations.
from cuprum.program import (
    Program,  # ruff: ignore[typing-only-first-party-import] - public annotations must resolve at runtime,
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
    relay_fallbacks:
        Handled echo-disablement records from this command's own streams, one
        per affected drain in stdout-then-stderr order. The ordering does not
        reconstruct chronological interleaving between the two streams. Empty
        when no echo write was handled as failed, and an echo failure does not
        change ``exit_code`` or ``ok``.

    """

    program: Program
    argv: tuple[str, ...]
    exit_code: int
    pid: int
    stdout: str | None
    stderr: str | None
    relay_fallbacks: tuple[RelayFallback, ...] = ()

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
    """Configure captured and mirrored command output.

    Parameters
    ----------
    capture : bool, default=True
        Store stdout and stderr on the returned result. Capture is independent
        of echoing, so a captured stream can remain silent and an echoed stream
        can be left uncaptured.
    echo : bool, default=False
        Shorthand for enabling both ``echo_stdout`` and ``echo_stderr`` unless
        either stream has an explicit override.
    echo_stdout : bool | None, default=None
        Whether stdout is mirrored to the execution context's stdout sink.
        ``None`` inherits ``echo``.
    echo_stderr : bool | None, default=None
        Whether stderr is mirrored to the execution context's stderr sink.
        ``None`` inherits ``echo``.
    max_echo_line_bytes : int | None, default=64 * 1024
        Inclusive byte bound for every echoed line, including its retained
        bytes, truncation marker, and terminator. ``None`` restores unbounded,
        chunk-for-chunk mirroring; captured output always remains complete.
    idle_after : float | None, default=None
        Seconds of silence, measured across both streams, before the run
        reports that it is still running. ``None`` disables idle reporting and
        costs nothing: no watchdog, no timer, no extra pipe. The interval must
        be finite and strictly positive. Reporting describes the absence of
        observed output — never a deadlock diagnosis — and can neither
        terminate the child nor extend its timeout.
    on_idle : cabc.Callable[[float, float], None] | None, default=None
        Synchronous ``(elapsed_total, elapsed_idle)`` callback, in seconds,
        invoked once per idle interval in place of the built-in stderr
        keepalive. It must not block for long: it runs on the run's own event
        loop. Requires ``idle_after``.

    Examples
    --------
    >>> options = RunOutputOptions(capture=True, echo=True)
    >>> options.resolved_echo
    (True, True)
    >>> RunOutputOptions(capture=True, echo=True, echo_stdout=False).resolved_echo
    (False, True)
    >>> RunOutputOptions(capture=False, idle_after=30.0).capture
    False
    """

    capture: bool = True
    echo: bool = False
    echo_stdout: bool | None = None
    echo_stderr: bool | None = None
    max_echo_line_bytes: int | None = DEFAULT_ECHO_MAX_LINE_BYTES
    idle_after: float | None = None
    on_idle: cabc.Callable[[float, float], None] | None = None

    def __post_init__(self) -> None:
        """Resolve per-stream echo from the ``echo`` shorthand."""
        object.__setattr__(
            self,
            "echo_stdout",
            self.echo if self.echo_stdout is None else self.echo_stdout,
        )
        object.__setattr__(
            self,
            "echo_stderr",
            self.echo if self.echo_stderr is None else self.echo_stderr,
        )
        # Stored normalized, so the schedule's arithmetic sees the float the
        # contract promises rather than whatever coerced to one here.
        object.__setattr__(
            self,
            "idle_after",
            _validate_idle_options(self.idle_after, self.on_idle),
        )

        if self.max_echo_line_bytes is None:
            return
        bound = self.max_echo_line_bytes
        is_positive_int = isinstance(bound, int) and not isinstance(bound, bool)
        if not is_positive_int or bound <= 0:
            msg = (
                "RunOutputOptions max_echo_line_bytes must be a positive "
                f"integer or None, got {bound!r}"
            )
            raise ValueError(msg)

    @property
    def resolved_echo(self) -> tuple[bool, bool]:
        """The resolved ``(echo_stdout, echo_stderr)`` gates.

        ``__post_init__`` fills any ``None`` per-stream field from ``echo``,
        so the returned pair is always concrete and reflects an explicit
        per-stream override where one was supplied.
        """
        # The dataclass is frozen and ``__post_init__`` resolves both fields,
        # but the declared field types stay ``bool | None``; the resolved pair
        # is the constructor's contract, not something the declared types can
        # express to the type checker.
        return (self.echo_stdout, self.echo_stderr)  # ty: ignore[invalid-return-type]


@dc.dataclass(frozen=True, slots=True)
class IOOptions(RunOutputOptions):
    """Deprecated alias for command output stream options."""

    def __post_init__(self) -> None:
        """Resolve the inherited options, then warn about the deprecated alias."""
        # Zero-arg ``super()`` breaks under ``slots=True``: the decorator
        # rebuilds the class, so the method's ``__class__`` cell refers to the
        # pre-rebuild class and the instance fails the ``super`` type check.
        RunOutputOptions.__post_init__(self)
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
            echo_stdout=output.resolved_echo[0],
            echo_stderr=output.resolved_echo[1],
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


# ruff: ignore[too-many-arguments]  # the six inputs are one run's resolved state, carried together rather than derived
def _build_subprocess_execution(
    cmd: SafeCmd,
    context: ExecutionContext,
    output: RunOutputOptions,
    *,
    timeout: float | None,
    observation: _StageObservation,
    stdin_data: bytes | None,
) -> _SubprocessExecution:
    """Bundle everything one command's execution needs, before it spawns.

    The idle monitor is part of the bundle rather than an execution-time
    argument because its presence is what decides whether the child's stdout
    and stderr are piped for activity observation. Deferring it would leave
    the spawn unable to make that choice.

    Returns
    -------
    _SubprocessExecution
        The resolved execution bundle, ready for ``_execute_with_hooks``.
    """
    return _SubprocessExecution(
        cmd=cmd,
        ctx=context,
        capture=output.capture,
        echo_stdout=output.resolved_echo[0],
        echo_stderr=output.resolved_echo[1],
        max_echo_line_bytes=output.max_echo_line_bytes,
        timeout=timeout,
        observation=observation,
        stdin_data=stdin_data,
        # Built here, during the parent's own preparation, but armed by the run
        # itself, once the child is actually running: everything that precedes
        # the spawn is the parent's work, and must not read as the child's
        # silence.
        idle=_build_idle_monitor(
            output.idle_after,
            output.on_idle,
            _idle_subject(str(cmd.program)),
            context.stderr_sink,
        ),
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
        timeout: float | None = None,  # ruff: ignore[async-function-with-timeout]  # ExecutionContext also supplies the timeout.
        context: ExecutionContext | None = None,
        stdin: StdinInput | None = None,
    ) -> CommandResult:
        """Execute the command asynchronously with predictable cancellation.

        Parameters
        ----------
        output : RunOutputOptions | None, default=None
            Capture and echo settings. Its 64 KiB default bounds each mirrored
            line without affecting capture; set ``max_echo_line_bytes=None``
            for unbounded mirroring.
        timeout : float | None, default=None
            Maximum execution time in seconds. An explicit value overrides the
            timeout in ``context``.
        context : ExecutionContext | None, default=None
            Execution settings, including echo sinks and text encoding.
        stdin : StdinInput | None, default=None
            Optional bytes or text supplied to the child process's stdin.

        Returns
        -------
        CommandResult
            The command outcome, including complete captured streams when
            ``output.capture`` is true.

        Raises
        ------
        PermissionError
            If the command is not allowed by the active scope.
        TimeoutError
            If execution exceeds the effective timeout.
        UnicodeEncodeError
            If text stdin cannot be encoded by the execution context.
        """  # ruff: ignore[docstring-extraneous-exception] - public exceptions propagate through execution helpers
        out = output or RunOutputOptions()
        ctx = context or ExecutionContext()
        _enforce_allowlist(self)
        stdin_data = stdin.resolve(ctx) if stdin is not None else None
        effective_timeout = _resolve_timeout(timeout=timeout, context=context)
        tracking = _ExecutionTracking(
            execution_hooks=_collect_hooks(current_context()),
            pending_tasks=[],
        )
        observation = _prepare_execution_observation(self, ctx, tracking, out)
        observation.emit("plan", _EventDetails(pid=None))
        for hook in tracking.execution_hooks.before_hooks:
            hook(self)
        return await _execute_with_hooks(
            self,
            _build_subprocess_execution(
                self,
                ctx,
                out,
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

        Parameters
        ----------
        output : RunOutputOptions | None, default=None
            Capture and echo settings. The default limits each mirrored line to
            64 KiB; ``max_echo_line_bytes=None`` restores unbounded echoing
            while preserving the same capture contract.
        timeout : float | None, default=None
            Maximum execution time in seconds.
        context : ExecutionContext | None, default=None
            Execution settings, including echo sinks and text encoding.
        stdin : StdinInput | None, default=None
            Optional bytes or text supplied to the child process's stdin.

        Returns
        -------
        CommandResult
            The command outcome, including complete captured streams when
            enabled.

        Raises
        ------
        PermissionError
            If the command is not allowed by the active scope.
        TimeoutError
            If execution exceeds the effective timeout.
        UnicodeEncodeError
            If text stdin cannot be encoded by the execution context.
        """  # ruff: ignore[docstring-extraneous-exception] - public exceptions propagate through run()
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
        timeout: float | None = None,  # ruff: ignore[async-function-with-timeout]  # ExecutionContext also supplies the timeout.
        context: ExecutionContext | None = None,
        **deprecated_flags: typ.Unpack[_DeprecatedOutputFlags],
    ) -> PipelineResult:
        """Execute the pipeline asynchronously with streaming and backpressure.

        Parameters
        ----------
        output : RunOutputOptions | None, default=None
            Capture and echo settings for every observed pipeline stream. The
            default bounds each echoed line to 64 KiB; ``None`` for
            ``max_echo_line_bytes`` restores unbounded mirroring without
            changing capture.
        timeout : float | None, default=None
            Maximum pipeline execution time in seconds.
        context : ExecutionContext | None, default=None
            Execution settings, including echo sinks and text encoding.
        **deprecated_flags : bool
            Deprecated ``capture`` and ``echo`` keyword arguments. Do not
            combine them with ``output``.

        Returns
        -------
        PipelineResult
            The outcome for every stage and complete captured streams when
            capture is enabled.

        Raises
        ------
        ValueError
            If ``output`` is combined with deprecated flags.
        PermissionError
            If a pipeline command is not allowed by the active scope.
        TimeoutError
            If execution exceeds the effective timeout.
        """  # ruff: ignore[docstring-extraneous-exception] - public exceptions propagate through pipeline helpers
        out = _resolve_pipeline_output(output, deprecated_flags)
        effective_timeout = _resolve_timeout(timeout=timeout, context=context)
        config = _prepare_pipeline_config(
            output=out,
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

        Parameters
        ----------
        output : RunOutputOptions | None, default=None
            Capture and echo settings. The 64 KiB default bounds mirrored lines;
            ``max_echo_line_bytes=None`` restores unbounded echoing while
            leaving captured output complete.
        timeout : float | None, default=None
            Maximum pipeline execution time in seconds.
        context : ExecutionContext | None, default=None
            Execution settings, including echo sinks and text encoding.
        **deprecated_flags : bool
            Deprecated ``capture`` and ``echo`` keyword arguments. Do not
            combine them with ``output``.

        Returns
        -------
        PipelineResult
            The outcome for every stage and complete captured streams when
            capture is enabled.

        Raises
        ------
        ValueError
            If ``output`` is combined with deprecated flags.
        PermissionError
            If a pipeline command is not allowed by the active scope.
        TimeoutError
            If execution exceeds the effective timeout.
        """  # ruff: ignore[docstring-extraneous-exception] - public exceptions propagate through run()
        out = _resolve_pipeline_output(output, deprecated_flags)
        return asyncio.run(
            self.run(output=out, timeout=timeout, context=context),
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
