"""Pipeline execution configuration helpers."""

from __future__ import annotations

import dataclasses as dc
import sys
import typing as typ

from cuprum._streams import _StreamConfig
from cuprum._streams_pump import _current_read_size

if typ.TYPE_CHECKING:
    from cuprum.lines import _LineHookFn
    from cuprum.sh import ExecutionContext, RunOutputOptions


@dc.dataclass(frozen=True, slots=True)
class _PipelineRunConfig:
    """Normalized runtime options for pipeline execution."""

    ctx: ExecutionContext

    capture: bool

    echo_stdout: bool

    echo_stderr: bool

    max_echo_line_bytes: int | None

    timeout: float | None

    stdout_sink: typ.IO[str]

    stderr_sink: typ.IO[str]
    on_line: _LineHookFn | None = None

    @property
    def stdout_capture_or_echo(self) -> bool:
        """Whether stdout must be consumed for capture or echo."""
        return self.capture or self.echo_stdout

    @property
    def stderr_capture_or_echo(self) -> bool:
        """Whether stderr must be consumed for capture or echo."""
        return self.capture or self.echo_stderr

    @property
    def stdout_consumed(self) -> bool:
        """Whether the final stage's stdout must be read at all.

        A registered ``on_line`` observes the final stage's stdout too, so it
        keeps the pipe and its consumer even when capture and echo are both off.
        """
        return self.stdout_capture_or_echo or self.on_line is not None

    @property
    def stderr_consumed(self) -> bool:
        """Whether every stage's stderr must be read at all."""
        return self.stderr_capture_or_echo or self.on_line is not None

    def stream_config(
        self,
        stream: typ.Literal["stdout", "stderr"],
    ) -> _StreamConfig:
        """Build the requested pipeline stream's capture and echo settings."""
        echo_output, sink = (
            (self.echo_stdout, self.stdout_sink)
            if stream == "stdout"
            else (self.echo_stderr, self.stderr_sink)
        )
        return _StreamConfig(
            capture_output=self.capture,
            echo_output=echo_output,
            echo_max_line_bytes=self.max_echo_line_bytes,
            sink=sink,
            encoding=self.ctx.encoding,
            errors=self.ctx.errors,
            read_size=_current_read_size(),
        )


def _prepare_pipeline_config(
    *,
    output: RunOutputOptions,
    timeout: float | None,
    context: ExecutionContext | None,
) -> _PipelineRunConfig:
    """Normalize runtime options for pipeline execution from one options object."""
    # Deferred, unlike the module-scope import in ``_pipeline_results``: this
    # module is imported by ``_pipeline_streams``, which ``_pipeline_collect``
    # imports, so hoisting the import would close the cycle rather than avoid
    # it.
    from cuprum._pipeline_collect import _sh_module

    sh = _sh_module()
    ctx = context or sh.ExecutionContext()
    stdout_sink = ctx.stdout_sink if ctx.stdout_sink is not None else sys.stdout
    stderr_sink = ctx.stderr_sink if ctx.stderr_sink is not None else sys.stderr
    # ``RunOutputOptions`` is the canonical carrier for output behaviour, so
    # the resolved gates are read straight off it rather than restated as
    # separate arguments; the developer guide forbids parallel internal
    # output-option objects.
    echo_stdout, echo_stderr = output.resolved_echo
    return _PipelineRunConfig(
        ctx=ctx,
        capture=output.capture,
        echo_stdout=echo_stdout,
        echo_stderr=echo_stderr,
        max_echo_line_bytes=output.max_echo_line_bytes,
        timeout=timeout,
        stdout_sink=stdout_sink,
        stderr_sink=stderr_sink,
        on_line=output.on_line,
    )
