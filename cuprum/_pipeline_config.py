"""Pipeline execution configuration helpers."""

from __future__ import annotations

import dataclasses as dc
import sys
import typing as typ

from cuprum._idle_diagnostic import _PIPELINE_IDLE_SUBJECT
from cuprum._idle_heartbeat import _build_idle_monitor
from cuprum._streams import _StreamConfig
from cuprum._streams_pump import _current_read_size

if typ.TYPE_CHECKING:
    from cuprum._idle_heartbeat import _IdleMonitor
    from cuprum._streams import _MirrorCursor
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
    idle: _IdleMonitor | None = None

    @property
    def consumes_stdout(self) -> bool:
        """Whether the parent must consume the final stage's stdout."""
        return self.capture or self.echo_stdout or self.idle is not None

    @property
    def consumes_stderr(self) -> bool:
        """Whether the parent must consume a stage's stderr."""
        return self.capture or self.echo_stderr or self.idle is not None

    @property
    def stream_config(self) -> _StreamConfig:
        """Build the stdout stream configuration for the final pipeline stage."""
        return _StreamConfig(
            capture_output=self.capture,
            echo_output=self.echo_stdout,
            echo_max_line_bytes=self.max_echo_line_bytes,
            sink=self.stdout_sink,
            encoding=self.ctx.encoding,
            errors=self.ctx.errors,
            read_size=_current_read_size(),
            activity=self.idle.note_activity if self.idle is not None else None,
            mirror=self._echo_mirror(self.stdout_sink),
        )

    @property
    def stderr_stream_config(self) -> _StreamConfig:
        """Build the stderr stream configuration for a pipeline stage."""
        return _StreamConfig(
            capture_output=self.capture,
            echo_output=self.echo_stderr,
            echo_max_line_bytes=self.max_echo_line_bytes,
            sink=self.stderr_sink,
            encoding=self.ctx.encoding,
            errors=self.ctx.errors,
            read_size=_current_read_size(),
            activity=self.idle.note_activity if self.idle is not None else None,
            mirror=self._echo_mirror(self.stderr_sink),
        )

    def _echo_mirror(self, sink: typ.IO[str]) -> _MirrorCursor | None:
        """Return the cursor for an echo whose sink is the keepalive's own.

        The cursor tracks where the keepalive's destination ended up, not which
        stream wrote there: a caller may point both sinks at one object, and
        then a newline-less final-stage stdout echo strands the diagnostic
        exactly as a stderr one would. Resolved sinks are compared, because
        that is where the bytes land.

        Returns
        -------
        _MirrorCursor | None
            The run's cursor when *sink* is the diagnostic destination, or
            ``None`` when this echo cannot reach the keepalive.
        """
        idle = self.idle
        if idle is None or sink is not self.stderr_sink:
            return None
        return idle.mirror


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
        # One aggregate heartbeat for the whole pipeline, labelled for what it
        # actually observes: the parent-facing output, not the health of every
        # stage. The clock starts when the first stage starts.
        idle=_build_idle_monitor(
            output.idle_after,
            output.on_idle,
            _PIPELINE_IDLE_SUBJECT,
            ctx.stderr_sink,
        ),
    )
