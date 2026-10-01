"""``Pipeline`` — stdout-to-stdin composition of ``SafeCmd`` stages.

``Pipeline`` chains curated commands so that each stage's stdout feeds the next
stage's stdin, and reports one ``PipelineResult`` for the whole chain. It lives
here rather than beside :class:`~cuprum.sh.safe_cmd.SafeCmd` because the
dependency between the two runs one way: a pipeline is built out of commands,
while a command needs the pipeline type only to answer ``|``, which
``SafeCmd.__or__`` resolves with a function-local import. The ``cuprum.sh``
package re-exports both.

The module also hosts the deprecated flat ``capture``/``echo`` keyword
resolution, ``_resolve_pipeline_output``, because ``Pipeline.run`` and
``run_sync`` are its only callers.
"""

# No ``from __future__ import annotations`` here either: the public signatures
# are introspected with ``typing.get_type_hints``, so annotations are evaluated
# eagerly and every name they use is a genuine runtime import. The forward
# references inside ``Pipeline``'s own class body are quoted.
import asyncio
import dataclasses as dc
import typing as typ
import warnings

from cuprum._pipeline_config import _prepare_pipeline_config
from cuprum._pipeline_internals import (
    _MIN_PIPELINE_STAGES,
    _run_pipeline,
)
from cuprum._sink_lifecycle import _outcome_for_error
from cuprum._subprocess_context import _resolve_timeout
from cuprum.sh.execution import ExecutionContext
from cuprum.sh.output import RunOutputOptions
from cuprum.sh.results import PipelineResult
from cuprum.sh.safe_cmd import SafeCmd

__all__ = [
    "Pipeline",
]


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


def _reject_stdio_targets(output: RunOutputOptions) -> None:
    """Refuse standard-stream targets a pipeline cannot honour.

    ``RunOutputOptions`` carries ``stdin``/``stdout``/``stderr`` targets
    wherever it is accepted, but a pipeline reads only the capture, echo,
    sink, idle, and line-hook fields: the stage wiring that would carry a
    target to the right stage does not exist. Accepting one would be the
    silent loss ``RunOutputOptions`` refuses everywhere else — the run
    succeeds, exits ``0``, and the file the caller named is never created.

    Refusing is deliberately the whole fix. Making a target *mean* something
    for a pipeline is a design decision about which stage a stream belongs to,
    and that decision belongs with the stage wiring rather than being guessed
    here.

    Parameters
    ----------
    output : RunOutputOptions
        The resolved options a pipeline run was given.

    Raises
    ------
    ValueError
        If any of the three standard streams names a target.
    """
    named = [
        stream
        for stream in ("stdin", "stdout", "stderr")
        if getattr(output, stream) is not None
    ]
    if not named:
        return
    msg = (
        f"Pipeline.run does not support RunOutputOptions "
        f"{', '.join(named)} targets; redirect a single SafeCmd instead"
    )
    raise ValueError(msg)


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
            If ``output`` is combined with deprecated flags, or if it names a
            ``stdin``/``stdout``/``stderr`` target, which pipeline stages do
            not honour.
        PermissionError
            If a pipeline command is not allowed by the active scope.
        TimeoutError
            If execution exceeds the effective timeout.
        """  # ruff: ignore[docstring-extraneous-exception] - public exceptions propagate through run()
        out = _resolve_pipeline_output(output, deprecated_flags)
        return asyncio.run(
            self.run(output=out, timeout=timeout, context=context),
        )
