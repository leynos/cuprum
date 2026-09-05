"""Pipeline execution configuration helpers."""

from __future__ import annotations

import dataclasses as dc
import sys
import typing as typ

from cuprum._streams import _StreamConfig

if typ.TYPE_CHECKING:
    from cuprum.sh import ExecutionContext


@dc.dataclass(frozen=True, slots=True)
class _PipelineStreamOptions:
    """Per-stream echo gates resolved from ``RunOutputOptions``."""

    echo_stdout: bool
    echo_stderr: bool


@dc.dataclass(frozen=True, slots=True)
class _PipelineRunConfig:
    """Normalized runtime options for pipeline execution."""

    ctx: ExecutionContext
    capture: bool
    echo_stdout: bool
    echo_stderr: bool
    timeout: float | None
    stdout_sink: typ.IO[str]
    stderr_sink: typ.IO[str]

    @property
    def stdout_capture_or_echo(self) -> bool:
        """Whether stdout must be consumed for capture or echo."""
        return self.capture or self.echo_stdout

    @property
    def stderr_capture_or_echo(self) -> bool:
        """Whether stderr must be consumed for capture or echo."""
        return self.capture or self.echo_stderr

    @property
    def stream_config(self) -> _StreamConfig:
        """Build the stdout stream configuration for the final pipeline stage."""
        return _StreamConfig(
            capture_output=self.capture,
            echo_output=self.echo_stdout,
            sink=self.stdout_sink,
            encoding=self.ctx.encoding,
            errors=self.ctx.errors,
        )

    @property
    def stderr_stream_config(self) -> _StreamConfig:
        """Build the stderr stream configuration for a pipeline stage."""
        return _StreamConfig(
            capture_output=self.capture,
            echo_output=self.echo_stderr,
            sink=self.stderr_sink,
            encoding=self.ctx.encoding,
            errors=self.ctx.errors,
        )


def _prepare_pipeline_config(
    *,
    capture: bool,
    output: _PipelineStreamOptions,
    timeout: float | None,
    context: ExecutionContext | None,
) -> _PipelineRunConfig:
    """Normalize runtime options for pipeline execution."""
    # Deferred, unlike the module-scope import in ``_pipeline_results``: this
    # module is imported by ``_pipeline_streams``, which ``_pipeline_collect``
    # imports, so hoisting the import would close the cycle rather than avoid
    # it.
    from cuprum._pipeline_collect import _sh_module

    sh = _sh_module()
    ctx = context or sh.ExecutionContext()
    stdout_sink = ctx.stdout_sink if ctx.stdout_sink is not None else sys.stdout
    stderr_sink = ctx.stderr_sink if ctx.stderr_sink is not None else sys.stderr
    return _PipelineRunConfig(
        ctx=ctx,
        capture=capture,
        echo_stdout=output.echo_stdout,
        echo_stderr=output.echo_stderr,
        timeout=timeout,
        stdout_sink=stdout_sink,
        stderr_sink=stderr_sink,
    )
