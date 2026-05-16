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
    """Normalised runtime options for pipeline execution."""

    ctx: ExecutionContext
    capture: bool
    echo: bool
    timeout: float | None
    stdout_sink: typ.IO[str]
    stderr_sink: typ.IO[str]

    @property
    def capture_or_echo(self) -> bool:
        return self.capture or self.echo

    @property
    def stream_config(self) -> _StreamConfig:
        return _StreamConfig(
            capture_output=self.capture,
            echo_output=self.echo,
            sink=self.stdout_sink,
            encoding=self.ctx.encoding,
            errors=self.ctx.errors,
        )


def _prepare_pipeline_config(
    *,
    capture: bool,
    echo: bool,
    timeout: float | None,
    context: ExecutionContext | None,
) -> _PipelineRunConfig:
    """Normalise runtime options for pipeline execution."""
    from cuprum._pipeline_internals import _sh_module

    sh = _sh_module()
    ctx = context or sh.ExecutionContext()
    stdout_sink = ctx.stdout_sink if ctx.stdout_sink is not None else sys.stdout
    stderr_sink = ctx.stderr_sink if ctx.stderr_sink is not None else sys.stderr
    return _PipelineRunConfig(
        ctx=ctx,
        capture=capture,
        echo=echo,
        timeout=timeout,
        stdout_sink=stdout_sink,
        stderr_sink=stderr_sink,
    )
