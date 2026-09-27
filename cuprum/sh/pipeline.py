"""``Pipeline`` — stdout-to-stdin composition of ``SafeCmd`` stages.

``Pipeline`` chains curated commands so that each stage's stdout feeds the next
stage's stdin, and reports one ``PipelineResult`` for the whole chain. It lives
here rather than beside :class:`~cuprum.sh.safe_cmd.SafeCmd` because the
dependency between the two runs one way: a pipeline is built out of commands,
while a command needs the pipeline type only to answer ``|``, which
``SafeCmd.__or__`` resolves with a function-local import. The ``cuprum.sh``
package re-exports both.
"""

# No ``from __future__ import annotations`` here either: the public signatures
# are introspected with ``typing.get_type_hints``, so annotations are evaluated
# eagerly and every name they use is a genuine runtime import. The forward
# references inside ``Pipeline``'s own class body are quoted.
import asyncio
import dataclasses as dc
import typing as typ

from cuprum._pipeline_config import _prepare_pipeline_config
from cuprum._pipeline_internals import (
    _MIN_PIPELINE_STAGES,
    _run_pipeline,
)
from cuprum._sink_lifecycle import _outcome_for_error
from cuprum._subprocess_context import _resolve_timeout
from cuprum.sh.execution import ExecutionContext
from cuprum.sh.output import (
    RunOutputOptions,
    _DeprecatedOutputFlags,
    _resolve_pipeline_output,
)
from cuprum.sh.results import PipelineResult
from cuprum.sh.safe_cmd import SafeCmd

__all__ = [
    "Pipeline",
]


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
