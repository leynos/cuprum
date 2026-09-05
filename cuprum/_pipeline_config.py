"""Pipeline execution configuration helpers."""

from __future__ import annotations

import dataclasses as dc
import sys
import typing as typ

from cuprum._streams import _StreamConfig

if typ.TYPE_CHECKING:
    from cuprum.sh import ExecutionContext


@dc.dataclass(frozen=True, slots=True)
class _PipelineRunConfig:
    """Normalized runtime options for pipeline execution."""

    ctx: ExecutionContext
    capture: bool
    echo: bool
    max_echo_line_bytes: int | None
    timeout: float | None
    stdout_sink: typ.IO[str]
    stderr_sink: typ.IO[str]

    @property
    def capture_or_echo(self) -> bool:
        """Whether output must be consumed for capture or echo."""
        return self.capture or self.echo

    @property
    def stream_config(self) -> _StreamConfig:
        """Build the stream configuration for the final pipeline stage."""
        return _StreamConfig(
            capture_output=self.capture,
            echo_output=self.echo,
            echo_max_line_bytes=self.max_echo_line_bytes,
            sink=self.stdout_sink,
            encoding=self.ctx.encoding,
            errors=self.ctx.errors,
        )


def _prepare_pipeline_config(
    *,
    capture: bool,
    echo: bool,
    max_echo_line_bytes: int | None,
    timeout: float | None,
    context: ExecutionContext | None,
) -> _PipelineRunConfig:
    """Normalize runtime options for pipeline execution.

    ``max_echo_line_bytes`` rides along as a keyword parameter rather than a
    position on ``ExecutionContext`` because it is an output contract, not a
    runtime environment knob.

    Returns
    -------
    _PipelineRunConfig
        The normalized configuration with sinks resolved from the context or
        the process defaults.
    """
    # Deferred, unlike the module-scope import in ``_pipeline_results``: this
    # module is imported by ``_pipeline_streams``, which ``_pipeline_collect``
    # imports, so hoisting the import would close the cycle rather than avoid
    # it.
    from cuprum._pipeline_collect import _sh_module

    sh = _sh_module()
    ctx = context or sh.ExecutionContext()
    stdout_sink = ctx.stdout_sink if ctx.stdout_sink is not None else sys.stdout
    stderr_sink = ctx.stderr_sink if ctx.stderr_sink is not None else sys.stderr
    # Keep the resolved sinks on the config: the drain loop needs them even
    # when *context* is None and the module defaults to ``sys.stdout`` or
    # ``sys.stderr`` at call time.
    return _PipelineRunConfig(
        ctx=ctx,
        capture=capture,
        echo=echo,
        max_echo_line_bytes=max_echo_line_bytes,
        timeout=timeout,
        stdout_sink=stdout_sink,
        stderr_sink=stderr_sink,
    )
