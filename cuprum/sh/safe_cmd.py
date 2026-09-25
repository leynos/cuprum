"""``SafeCmd`` and ``Pipeline`` execution primitives for ``cuprum.sh``.

``SafeCmd`` is the typed, immutable curated command; ``Pipeline`` composes
``SafeCmd`` stages piped stdout-to-stdin. They reference each other at
runtime (``SafeCmd.__or__`` builds a ``Pipeline``), so they stay in one module
to avoid a runtime import cycle. The ``cuprum.sh`` package re-exports
both. ``SafeCmdBuilder`` lives in ``cuprum/sh/builder.py``, which imports
``SafeCmd`` from here; the reverse import would close that cycle.
"""

# No ``from __future__ import annotations`` here: the public signatures are
# introspected with ``typing.get_type_hints``, so annotations are evaluated
# eagerly and every name they use is a genuine runtime import. Only the forward
# references to ``SafeCmd`` and ``Pipeline`` inside their own class bodies are
# quoted.
import asyncio
import dataclasses as dc
import typing as typ

from cuprum._command_internals import (
    _build_subprocess_execution,
    _ExecutionState,
    _prepare_execution_observation,
    _run_prepared_command,
)
from cuprum._execution_tracking import _ExecutionTracking
from cuprum._line_iteration import LineStream, _iter_line_events
from cuprum._pipeline_config import _prepare_pipeline_config
from cuprum._pipeline_internals import (
    _MIN_PIPELINE_STAGES,
    _collect_hooks,
    _enforce_allowlist,
    _run_pipeline,
)
from cuprum._sink_lifecycle import _outcome_for_error, _SinkBracket
from cuprum._subprocess_context import _resolve_timeout
from cuprum.catalogue import ProjectSettings
from cuprum.context import current_context
from cuprum.program import Program
from cuprum.sh.execution import ExecutionContext, StdinInput
from cuprum.sh.output import (
    RunOutputOptions,
    _DeprecatedOutputFlags,
    _resolve_pipeline_output,
)
from cuprum.sh.results import CommandResult, PipelineResult

__all__ = [
    "Pipeline",
    "SafeCmd",
]


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

    def __or__(self, other: "SafeCmd | Pipeline") -> "Pipeline":
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
        return await _run_prepared_command(
            self,
            _ExecutionState(
                context=ctx,
                output=out,
                stdin_data=stdin_data,
                timeout=effective_timeout,
            ),
        )

    def lines(
        self,
        *,
        output: RunOutputOptions | None = None,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
        stdin: StdinInput | None = None,
    ) -> LineStream:
        """Iterate the command's output lines as they arrive.

        Line events are delivered in arrival order per stream, stamped with
        monotonic seconds since the command started. Capture and echo stay
        governed by *output* independently: iterating lines does not disable
        either unless the caller asks.

        Parameters
        ----------
        output:
            Optional ``RunOutputOptions`` controlling stdout/stderr handling.
        timeout:
            Optional wall-clock timeout in seconds; ``None`` disables timeouts.
            Expiry terminates the subprocess exactly as ``run()`` does.
        context:
            Optional execution settings such as env, cwd, and cancel grace.
        stdin:
            Optional ``StdinInput`` data to feed to the subprocess.

        Returns
        -------
        LineStream
            An async iterator of ``LineEvent`` whose ``result`` attribute
            holds the final ``CommandResult`` once iteration completes.

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
            # Line iteration never opens a presentation session: the line
            # events are the caller's own consumption of the streams, so there
            # is no adapter framing to bracket. The empty bracket keeps the
            # required field satisfied.
            sink_bracket=_SinkBracket(None),
        )
        observation = _prepare_execution_observation(self, ctx, tracking, out)

        return LineStream(
            _iter_line_events(
                _build_subprocess_execution(
                    self,
                    _ExecutionState(
                        context=ctx,
                        output=out,
                        stdin_data=stdin_data,
                        timeout=effective_timeout,
                    ),
                    observation=observation,
                ),
                tracking,
            ),
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

    def __or__(self, other: "SafeCmd | Pipeline") -> "Pipeline":
        """Compose pipelines, appending stages in left-to-right order."""
        return Pipeline.concat(self, other)

    @classmethod
    def concat(
        cls,
        left: "SafeCmd | Pipeline",
        right: "SafeCmd | Pipeline",
    ) -> "Pipeline":
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
        # The bracket opened with the config; this guard is the last word on
        # every path out of the pipeline, including one the runner itself
        # raises on the way to its first stage.
        try:
            return await _run_pipeline(self.parts, config)
        except BaseException as run_error:
            config.sink_bracket.close(outcome=_outcome_for_error(run_error))
            raise

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
