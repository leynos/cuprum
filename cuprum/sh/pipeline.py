"""The pipeline execution primitive.

Split out of the former ``cuprum/sh/safe_cmd.py`` module so each of the two
execution primitives owns a module: their byte-exact entry points are ordinary
methods of the class they extend, and keeping the class next to those methods
is what lets the methods annotate ``self`` implicitly rather than naming the
class in a string that only ``TYPE_CHECKING`` could resolve. The modules still
import each other — a command composes into a pipeline and a pipeline is built
from commands — so the cycle between them is broken the same way ``cuprum.sh``
breaks its own: each side defers the import it needs to the point of use.

``cuprum/sh/__init__.py`` re-exports both classes unchanged, so importers of
``cuprum.sh`` see the same objects they always did.

The output-option helpers this class calls — the deprecated flat
``capture``/``echo`` resolution and the refusal of standard-stream targets —
live in ``cuprum.sh.pipeline_output``. They are re-exported here because
``cuprum.sh`` and its callers import them from this module's path.
"""

# No ``from __future__ import annotations`` here: the public signatures are
# introspected with ``typing.get_type_hints``, so annotations are evaluated
# eagerly and every name they use is a genuine runtime import. Only the forward
# references to ``SafeCmd`` are quoted.
import dataclasses as dc
import typing as typ

from cuprum._bytes_run import (
    _bytes_output,
    _require_bytes_pipeline_result,
    _require_pipeline_result,
    _validate_bytes_output,
)
from cuprum._pipeline_config import _prepare_pipeline_config
from cuprum._pipeline_internals import _MIN_PIPELINE_STAGES, _run_pipeline
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._sink_lifecycle import _outcome_for_error
from cuprum._subprocess_context import _resolve_timeout
from cuprum.sh._sync_entry import _run_sync
from cuprum.sh.execution import ExecutionContext
from cuprum.sh.output import RunOutputOptions
from cuprum.sh.pipeline_output import (
    _DeprecatedOutputFlags as _DeprecatedOutputFlags,
)
from cuprum.sh.pipeline_output import (
    _reject_stdio_targets as _reject_stdio_targets,
)
from cuprum.sh.pipeline_output import (
    _resolve_pipeline_output as _resolve_pipeline_output,
)
from cuprum.sh.results import BytesPipelineResult, PipelineResult

__all__ = ["Pipeline"]


@dc.dataclass(frozen=True, slots=True)
class Pipeline:
    """A sequence of SafeCmd stages connected via stdout/stdin piping.

    Attributes
    ----------
    parts : tuple[SafeCmd, ...]
        The stages in execution order. The first stage's stdin is the
        pipeline's stdin and the last stage's stdout is its stdout; each
        intervening stage feeds the next. Always at least
        ``_MIN_PIPELINE_STAGES`` long, as :meth:`__post_init__` enforces.
    """

    parts: "tuple[SafeCmd, ...]"

    def __post_init__(self) -> None:
        """Validate stage count invariants."""
        if len(self.parts) < _MIN_PIPELINE_STAGES:
            msg = "Pipeline must contain at least two stages"
            raise ValueError(msg)

    def __or__(self, other: "SafeCmd | Pipeline") -> "Pipeline":
        """Compose this pipeline with another stage.

        Parameters
        ----------
        other : SafeCmd | Pipeline
            The stage, or sequence of stages, appended after this pipeline's.

        Returns
        -------
        Pipeline
            A pipeline whose stages are this one's followed by *other*'s.
        """
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

        Raises
        ------
        TypeError
            If either operand is neither a :class:`SafeCmd` nor a
            :class:`Pipeline`. Structural matching cannot express
            exhaustiveness, so an unhandled operand would otherwise fall out
            of the ``match`` and return ``None`` from a function that promises
            a pipeline, deferring the failure to a distant attribute access.
        """
        match left, right:
            case Pipeline(parts=left_parts), Pipeline(parts=right_parts):
                return cls((*left_parts, *right_parts))
            case Pipeline(parts=left_parts), SafeCmd() as right_stage:
                return cls((*left_parts, right_stage))
            case SafeCmd() as left_stage, Pipeline(parts=right_parts):
                return cls((left_stage, *right_parts))
            case SafeCmd() as left_stage, SafeCmd() as right_stage:
                return cls((left_stage, right_stage))
            case _:
                msg = (
                    "pipeline stages must be SafeCmd or Pipeline, got "
                    f"{type(left).__name__} and {type(right).__name__}"
                )
                raise TypeError(msg)

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
            If ``output`` is combined with deprecated flags, or if it names a
            ``stdin``/``stdout``/``stderr`` target, which pipeline stages do
            not honour.
        PermissionError
            If a pipeline command is not allowed by the active scope.
        TimeoutError
            If execution exceeds the effective timeout.
        """  # ruff: ignore[docstring-extraneous-exception] - public exceptions propagate through pipeline helpers
        out = _resolve_pipeline_output(output, deprecated_flags)
        _reject_stdio_targets(out)
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
            return _require_pipeline_result(await _run_pipeline(self.parts, config))
        except BaseException as run_error:
            await _shielded_cleanup(
                config.sink_bracket.aclose(outcome=_outcome_for_error(run_error))
            )
            raise

    def run_sync(
        self,
        *,
        output: RunOutputOptions | None = None,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
        **deprecated_flags: typ.Unpack[_DeprecatedOutputFlags],
    ) -> PipelineResult:
        """Execute the pipeline synchronously on a loop this call owns.

        The call creates and closes a fresh event loop, so it cannot run while
        another event loop is running in the calling thread. Await :meth:`run`
        from async code instead.

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
        RunningEventLoopError
            If an event loop is already running in the calling thread. The
            error names :meth:`run` as the asynchronous alternative.
        ValueError
            If ``output`` is combined with deprecated flags, or if it names a
            ``stdin``/``stdout``/``stderr`` target, which pipeline stages do
            not honour.
        PermissionError
            If a pipeline command is not allowed by the active scope.
        TimeoutError
            If execution exceeds the effective timeout.
        """  # ruff: ignore[docstring-extraneous-exception] - public exceptions propagate through run()
        out = _resolve_pipeline_output(output, deprecated_flags)
        return _run_sync(
            lambda: self.run(output=out, timeout=timeout, context=context),
            entry_point="run_sync()",
            async_alternative="run()",
        )

    async def run_bytes(
        self,
        *,
        output: RunOutputOptions | None = None,
        timeout: float | None = None,  # ruff: ignore[async-function-with-timeout]  # ExecutionContext also supplies the timeout.
        context: ExecutionContext | None = None,
    ) -> BytesPipelineResult:
        """Execute the pipeline, capturing its output as bytes.

        Every stage runs exactly as it does under ``run()``; the difference is
        what the parent keeps. Each stage's captured stderr and the final
        stage's captured stdout are reported as the child wrote them, which is
        what lets a pipeline relay a binary payload end to end. An interior
        stage's stdout is consumed by the next stage rather than captured, so
        it is reported as ``None`` exactly as it is in text mode.

        The pipeline's own stdin feeding is unchanged: the first stage reads
        from the parent process's stdin, as it always does, and no stage's
        output is interpreted on the way through.

        Parameters
        ----------
        output : RunOutputOptions | None, default=None
            Capture and echo settings for every observed pipeline stream.
            ``on_line`` is rejected: bytes mode returns each stream's own
            bytes, so a callback carrying decoded text would be a second,
            contradictory contract for the same stream. Use the text-mode
            entry point when lines are wanted.
        timeout : float | None, default=None
            Maximum pipeline execution time in seconds.
        context : ExecutionContext | None, default=None
            Execution settings, including echo sinks and text encoding.

        Returns
        -------
        BytesPipelineResult
            Per-stage outcomes plus the final stage's captured stdout, as
            bytes wherever a stream was captured.

        Raises
        ------
        ValueError
            If ``output.on_line`` is set.
        PermissionError
            If a pipeline command is not allowed by the active scope.
        TimeoutError
            If execution exceeds the effective timeout.

        """  # ruff: ignore[docstring-extraneous-exception] - ValueError and the public exceptions propagate through the bytes entry point
        out = _bytes_output(output)
        _validate_bytes_output(out)
        effective_timeout = _resolve_timeout(timeout=timeout, context=context)
        config = _prepare_pipeline_config(
            output=out,
            timeout=effective_timeout,
            context=context,
            capture_bytes=True,
        )
        # The bracket opened with the config; this guard is the last word on
        # every path out of the pipeline, including one the runner itself
        # raises on the way to its first stage.
        try:
            return _require_bytes_pipeline_result(
                await _run_pipeline(self.parts, config)
            )
        except BaseException as run_error:
            await _shielded_cleanup(
                config.sink_bracket.aclose(outcome=_outcome_for_error(run_error))
            )
            raise

    def run_bytes_sync(
        self,
        *,
        output: RunOutputOptions | None = None,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
    ) -> BytesPipelineResult:
        """Execute the pipeline synchronously on a loop this call owns.

        The call creates and closes a fresh event loop, so it cannot run while
        another event loop is running in the calling thread. Await
        :meth:`run_bytes` from async code instead.

        Parameters
        ----------
        output : RunOutputOptions | None, default=None
            Capture and echo settings. ``on_line`` is rejected: bytes mode
            returns each stream's own bytes, so a callback carrying decoded
            text would be a second, contradictory contract for the same
            stream. Use the text-mode entry point when lines are wanted.
        timeout : float | None, default=None
            Maximum pipeline execution time in seconds.
        context : ExecutionContext | None, default=None
            Execution settings, including echo sinks and text encoding.

        Returns
        -------
        BytesPipelineResult
            Per-stage outcomes plus the final stage's captured stdout, as
            bytes wherever a stream was captured.

        Raises
        ------
        RunningEventLoopError
            If an event loop is already running in the calling thread. The
            error names :meth:`run_bytes` as the asynchronous alternative.
        ValueError
            If ``output.on_line`` is set.
        PermissionError
            If a pipeline command is not allowed by the active scope.
        TimeoutError
            If execution exceeds the effective timeout.

        """  # ruff: ignore[docstring-extraneous-exception] - ValueError and the public exceptions propagate through the bytes entry point
        return _run_sync(
            lambda: self.run_bytes(output=output, timeout=timeout, context=context),
            entry_point="run_bytes_sync()",
            async_alternative="run_bytes()",
        )


# Imported at the foot of the module, after ``Pipeline`` exists, to break the
# mutual dependency with ``cuprum.sh.safe_cmd``: that module imports this one
# for ``Pipeline``, so importing it at the head would leave it reading a
# half-built module. The annotations above name ``SafeCmd`` in strings, but
# ``typing.get_type_hints`` resolves them from this module's globals, and the
# ``concat`` match arms use the name at run time to match a stage operand —
# both of which make this a genuine runtime import rather than a
# ``TYPE_CHECKING`` one.
from cuprum.sh.safe_cmd import (  # ruff: ignore[module-import-not-at-top-of-file]  # deferred to break the cuprum.sh.safe_cmd dependency cycle
    SafeCmd,
)
