"""Pipeline execution configuration helpers.

``_PipelineRunConfig`` carries the resolved ``RunOutputOptions``, including
``on_line``, alongside context-derived stream settings. Its ``consumes_stdout``
and ``consumes_stderr`` decisions keep a stream readable when capture, echo,
line observation, or the idle heartbeat needs it, and the ``stream_config`` and
``stderr_stream_config`` fields supply the stream-specific capture, echo, sink,
encoding, and error configuration consumed by pipeline stream tasks.

Both stream configurations are built once, by ``__post_init__``, rather than
per read. That is not merely an economy: a stream config carries the capture
accumulator for its own policy, so a run that rebuilt them would hand its
consumers accumulators no other party could reach, and the spill files those
accumulators own would outlive the run that created them.
"""

from __future__ import annotations

import dataclasses as dc
import sys
import typing as typ

from cuprum._bounded_encoding import _reject_unsupported_bounded_encoding
from cuprum._idle_diagnostic import _PIPELINE_IDLE_SUBJECT
from cuprum._idle_heartbeat import _build_idle_monitor
from cuprum._sink_lifecycle import _SinkBracket
from cuprum._streams import _StreamConfig
from cuprum._streams_pump import _current_read_size
from cuprum.capture_presence import captures_any_stream as _captures_any_stream
from cuprum.echo_events import BrokenPipePolicy

if typ.TYPE_CHECKING:
    from cuprum._idle_heartbeat import _IdleMonitor
    from cuprum._streams import _MirrorCursor
    from cuprum.capture_policy import CapturePolicy
    from cuprum.lines import _LineHookFn
    from cuprum.sh import ExecutionContext, RunOutputOptions

from cuprum.sinks import base as sinks


@dc.dataclass(frozen=True, slots=True)
class _PipelineRunConfig:
    """Normalized runtime options for pipeline execution."""

    ctx: ExecutionContext

    # One resolved policy per stream, so a caller can bound the final stage's
    # stdout without bounding every stage's stderr, or spill one and not the
    # other. ``None`` means that stream is not captured at all. The policies
    # are resolved by ``RunOutputOptions``, so the shorthand ``capture`` bool
    # the caller may have spelled is already turned into a policy here.
    capture_stdout: CapturePolicy | None

    capture_stderr: CapturePolicy | None

    echo_stdout: bool

    echo_stderr: bool

    max_echo_line_bytes: int | None

    timeout: float | None

    stdout_sink: typ.IO[str]

    stderr_sink: typ.IO[str]
    sink_bracket: _SinkBracket

    on_line: _LineHookFn | None = None

    # One policy for the whole pipeline, inherited by every stage's stdout and
    # stderr config: the caller named it once on the options object, and a
    # stage is not the place to second-guess which of its streams may tolerate
    # a closed reader. Defaulted for the callers that build this config
    # directly; the production path always resolves it from the options.
    broken_pipe_policy: BrokenPipePolicy = BrokenPipePolicy.STRICT
    # Whether the pipeline's captured streams are reported as bytes rather
    # than decoded text. Like the broken-pipe policy, one value governs the
    # whole pipeline: the caller asked for it once, and the internal stage
    # boundaries are not the place to decide per stream.
    capture_bytes: bool = False

    idle: _IdleMonitor | None = None

    # Built once, here, because each one owns the capture accumulator its
    # policy selected. A run whose consumers built their own would leave the
    # accumulators holding the spill files unreachable from teardown, so the
    # files would outlive the run. Both are derived state, so neither takes
    # part in equality or the representation.
    stream_config: _StreamConfig = dc.field(init=False, repr=False, compare=False)
    stderr_stream_config: _StreamConfig = dc.field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Build both streams' configurations, and their accumulators, once."""
        object.__setattr__(
            self,
            "stream_config",
            self._build_stream_config(
                capture=self.capture_stdout,
                echo_output=self.echo_stdout,
                fallback_sink=self.stdout_sink,
            ),
        )
        object.__setattr__(
            self,
            "stderr_stream_config",
            self._build_stream_config(
                capture=self.capture_stderr,
                echo_output=self.echo_stderr,
                fallback_sink=self.stderr_sink,
            ),
        )

    @property
    def captures_any_stream(self) -> bool:
        """Whether the pipeline captures at least one of the two streams.

        The observation tag reports "capture" as one bool, and this is the
        reading that keeps its meaning under per-stream policies: a run that
        captures anything at all is a capturing run, and a run that names no
        policy for either stream captures nothing.
        """
        return _captures_any_stream(self.capture_stdout, self.capture_stderr)

    @property
    def consumes_stdout(self) -> bool:
        """Whether the parent must consume the final stage's stdout.

        A registered ``on_line`` observes the final stage's stdout too, so it
        keeps the pipe and its consumer even when capture and echo are both
        off, and the idle heartbeat needs raw chunks for the same reason.
        """
        return (
            self.capture_stdout is not None
            or self.echo_stdout
            or self.idle is not None
            or self.on_line is not None
        )

    @property
    def consumes_stderr(self) -> bool:
        """Whether the parent must consume a stage's stderr.

        Every stage's stderr is line-observed by the caller's ``on_line``, so
        the same gates that keep stdout readable apply here.
        """
        return (
            self.capture_stderr is not None
            or self.echo_stderr
            or self.idle is not None
            or self.on_line is not None
        )

    def _build_stream_config(
        self,
        *,
        capture: CapturePolicy | None,
        echo_output: bool,
        fallback_sink: typ.IO[str],
    ) -> _StreamConfig:
        """Build one parent-consumed pipeline stream configuration.

        Both pipeline streams are wired identically here; only the capture
        policy, the echo gate, and the resolved destination differ. When a
        presentation-sink session is active, a mirrored stream routes through
        the session's log destination instead of its fallback, so it lands
        inside the adapter's framing in the order the adapter received it. That
        bracketing is required for stdout and stderr alike: every mirrored
        stream must fall between the session's opening and closing frames.

        Parameters
        ----------
        capture : CapturePolicy | None
            This stream's resolved capture policy, or ``None`` for a stream
            the run is not capturing.
        echo_output : bool
            Whether this stream's lines are mirrored to the parent.
        fallback_sink : typ.IO[str]
            The stream's resolved parent-facing destination, superseded by the
            active session's log when one is open.

        Returns
        -------
        _StreamConfig
            The wiring for this stream, framed by any active sink session.
        """
        framed = self.sink_bracket.resolve_destination(fallback_sink)
        return _StreamConfig(
            capture=capture,
            echo_output=echo_output,
            echo_max_line_bytes=self.max_echo_line_bytes,
            broken_pipe_policy=self.broken_pipe_policy,
            sink=framed,
            encoding=self.ctx.encoding,
            errors=self.ctx.errors,
            capture_bytes=self.capture_bytes,
            read_size=_current_read_size(),
            activity=self.idle.note_activity if self.idle is not None else None,
            mirror=self._echo_mirror(framed),
        )

    def _echo_mirror(self, sink: typ.IO[str]) -> _MirrorCursor | None:
        """Return the cursor for an echo whose sink is the keepalive's own.

        The cursor tracks where the keepalive's destination ended up, not which
        stream wrote there: a caller may point both sinks at one object, and
        then a newline-less final-stage stdout echo strands the diagnostic
        exactly as a stderr one would. Resolved sinks are compared, because
        that is where the bytes land: when a session is active both route
        through the session's log, and the mirrored stream is still the one
        whose unfinished line the diagnostic would otherwise extend.

        Parameters
        ----------
        sink : typ.IO[str]
            The echo's resolved destination, already framed by the sink
            bracket.

        Returns
        -------
        _MirrorCursor | None
            The run's cursor when *sink* is the diagnostic destination, or
            ``None`` when this echo cannot reach the keepalive.
        """
        idle = self.idle
        if idle is None:
            return None
        framed_stderr = self.sink_bracket.resolve_destination(self.stderr_sink)
        return None if sink is not framed_stderr else idle.mirror


def _prepare_pipeline_config(
    *,
    output: RunOutputOptions,
    timeout: float | None,
    context: ExecutionContext | None,
    capture_bytes: bool = False,
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
    capture_stdout, capture_stderr = output.resolved_capture
    # Rejected here, before any stage is spawned. The encoding is the run's,
    # shared by every stage, so one refusal covers the whole pipeline; a
    # bounded text capture that reached the drain under an unrepairable codec
    # would decode an orphaned trail byte as a character the child never wrote.
    for capture in (capture_stdout, capture_stderr):
        _reject_unsupported_bounded_encoding(
            capture,
            encoding=ctx.encoding,
            capture_bytes=capture_bytes,
        )
    sink_bracket = _SinkBracket.open(
        output.sink,
        sinks.SessionStart(label="pipeline", argv=()),
    )
    return _PipelineRunConfig(
        ctx=ctx,
        capture_stdout=capture_stdout,
        capture_stderr=capture_stderr,
        echo_stdout=echo_stdout,
        echo_stderr=echo_stderr,
        max_echo_line_bytes=output.max_echo_line_bytes,
        broken_pipe_policy=output.resolved_broken_pipe_policy,
        timeout=timeout,
        stdout_sink=stdout_sink,
        stderr_sink=stderr_sink,
        sink_bracket=sink_bracket,
        capture_bytes=capture_bytes,
        on_line=output.on_line,
        # One aggregate heartbeat for the whole pipeline, labelled for what it
        # actually observes: the parent-facing output, not the health of every
        # stage. The clock starts when the first stage starts.
        idle=_build_idle_monitor(
            output.idle_after,
            output.on_idle,
            _PIPELINE_IDLE_SUBJECT,
            # The same resolution the mirrored streams use: an active session
            # frames them, so a keepalive written anywhere else would land
            # outside the group the run is claiming. One resolution, not two,
            # because _echo_mirror recognizes the diagnostic by comparing the
            # resolved destinations.
            sink_bracket.resolve_destination(stderr_sink),
        ),
    )
