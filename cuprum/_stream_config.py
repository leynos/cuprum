"""The per-stream configuration one drain reads.

``_StreamConfig`` is the value a drain is handed: which capture policy retains
this stream, whether it is echoed and where, how its bytes decode, and the
run-owned observers that watch it. Everything a consumer needs to know about
one of a child's two streams is decided here, once, before the first chunk is
read, so the drain loop never branches on policy per chunk.

The accumulator a capture policy retains into is *derived* here rather than
passed in, and that is the load-bearing choice this module makes. A drain
cancelled while awaiting its line sink never reaches a drain result, and the
accumulator would then be the last reference to a spill file nothing could
remove. Deriving it at construction is what makes the file reachable from the
run's single reconciliation point: the stderr config is built by replacing the
stdout one, so it picks up an accumulator for its own policy without any caller
having to remember to build one.

``cuprum._streams`` re-exports the type, so importers of that module keep
working unchanged. This module is a leaf: it depends on the policy types, the
accumulator factory, and the echo/encode bounds, and nothing depends on it
except through the re-export.
"""

from __future__ import annotations

import dataclasses as dc
import sys
import typing as typ

from cuprum._capture_accumulators import build_accumulator
from cuprum._capture_decode import _DecodeSettings
from cuprum._constants import OBSERVER_ERROR_POLICY
from cuprum._streams_pump import _READ_SIZE
from cuprum.echo_events import BrokenPipePolicy, EchoStream

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc

    from cuprum._capture_types import CaptureSink
    from cuprum._stream_drain_state import _MirrorCursor
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.capture_policy import CapturePolicy
    from cuprum.sinks.base import OutputSession


def _resolve_stream_sink(
    session: OutputSession | None,
    configured: typ.IO[str] | None,
    fallback: typ.IO[str],
) -> typ.IO[str]:
    """Return the sink echoed output for one stream is written to.

    A live presentation-sink session normally owns the destination, so
    mirrored output lands inside the adapter's framing — the GitHub Actions
    group, say — in the order the adapter received it. A session exposing
    ``redirects_echo = False`` leaves echo on the caller-configured sink, or
    the process's own stream when no sink was configured.

    This is the middle rung of one resolution the whole run shares: the
    sibling :meth:`cuprum._sink_lifecycle._SinkBracket.resolve_destination`
    answers the session half for destinations that have no caller-configured
    sink between them, and the pipeline config composes that half with the
    caller's choice made here.

    It lives beside the config it feeds, since resolving a destination is the
    first half of deriving a stream's configuration.

    Returns
    -------
    typ.IO[str]
        The destination for this stream's echoed output.
    """
    if session is not None and getattr(session, "redirects_echo", True) is not False:
        return session.log
    return fallback if configured is None else configured


def _resolve_stream_configs(
    execution: _SubprocessExecution,
    stdout_config: _StreamConfig,
) -> tuple[_StreamConfig, _StreamConfig]:
    """Derive the stderr config, and the stdout config's final cursor.

    Each stream's *own* settings are applied here — its capture policy, its
    echo gate, and its resolved sink — because those differ per stream. What
    the two share is derived by replacement: the mode, the encoding, and the
    broken-pipe policy are properties of the run, so one value governs both.

    The cursor is the one place the two streams interact. It tracks where the
    keepalive's destination ended up, not which stream wrote there: a caller
    may point both sinks at one object, and then a newline-less stdout echo
    strands the diagnostic exactly as a stderr one would. Resolved sinks are
    compared, because that is where the bytes land.

    It lives beside the config it builds rather than beside the spawn that
    asks for it, so a change to how a config is derived cannot outgrow the
    module that defines what a config *is*.

    Returns
    -------
    tuple[_StreamConfig, _StreamConfig]
        The stdout and stderr configurations, in that order, ready to be
        drained and to have their accumulators cleaned up.
    """
    stderr_sink = _resolve_stream_sink(
        execution.sink_session, execution.ctx.stderr_sink, sys.stderr
    )
    stderr_config = dc.replace(
        stdout_config,
        capture=execution.capture_stderr,
        echo_output=execution.echo_stderr,
        sink=stderr_sink,
        stream=EchoStream.STDERR,
        mirror=execution.idle.mirror if execution.idle is not None else None,
    )
    if stdout_config.sink is stderr_config.sink:
        stdout_config = dc.replace(stdout_config, mirror=stderr_config.mirror)
    return stdout_config, stderr_config


@dc.dataclass(frozen=True, slots=True)
class _StreamConfig:
    """Configuration for decoding and echoing a subprocess stream."""

    # The resolved per-stream capture policy, or ``None`` for a stream this
    # run does not capture. A policy rather than a bool so the drain can build
    # the right accumulator once, at drain start, and never branch per chunk.
    capture: CapturePolicy | None
    echo_output: bool
    sink: typ.IO[str]
    encoding: str
    errors: str
    # Profiled private read size; production callers use the default.
    read_size: int = _READ_SIZE
    # Byte bound for each line mirrored to the echo sink; ``None`` keeps the
    # raw chunk-for-chunk echo. Bounded echoing protects consumers that stop
    # accepting a line past a size limit (GitHub Actions job logs end at a
    # 64 KiB line) while capture stays byte-for-byte complete.
    echo_max_line_bytes: int | None = None
    discard_on_cancel: asyncio.Event | None = None
    # How a broken pipe on this stream's sink is handled. Defaults to strict
    # propagation, so a config built without naming a policy keeps the
    # behaviour every caller had before the policy existed.
    broken_pipe_policy: BrokenPipePolicy = BrokenPipePolicy.STRICT
    # Which output stream this config drains, for bounded echo observability.
    # Defaults to stdout because every production call site names the stderr
    # config explicitly when it replaces the stdout one.
    stream: EchoStream = EchoStream.STDOUT
    # Whether the captured payload is reported as the child's own bytes rather
    # than decoded text. Off by default, so every config built before byte-exact
    # capture existed keeps decoding exactly as it did. When on, the drain still
    # echoes through the ordinary renderer — a sink that accepts bytes gets
    # them, a text sink decodes for display only — but the value handed back is
    # never decoded, so arbitrary binary survives the round trip.
    capture_bytes: bool = False
    # Run-owned observers, both optional and both unable to change what is
    # captured: ``activity`` reports that a non-empty chunk arrived, before any
    # decoding, truncation, or line callback could drop it, and ``mirror``
    # records where the echo sink ended up so a keepalive written later knows
    # whether it would land mid-line.
    activity: cabc.Callable[[], None] | None = None
    mirror: _MirrorCursor | None = None
    # The accumulator *capture* is retained in, derived from the policy, mode,
    # and encoding above rather than passed in. Held on the config rather than
    # built inside the drain because a drain cancelled while awaiting its line
    # sink never reaches a drain result, and the sink would then be the last
    # reference to a spill file nothing could remove. Deriving it here is what
    # makes that reachable: the stderr config is built by replacing the stdout
    # one, so it picks up an accumulator for its own policy without any caller
    # having to remember to build one.
    #
    # Derived state, so it takes no part in equality or the representation, as
    # ``_PipelineRunConfig``'s configs do. Equality would otherwise depend on
    # which policy built the sink — two accumulators compare by identity, while
    # ``None`` compares equal — and a bounded accumulator is unhashable, so
    # hashing a config would raise only under that one policy. The repr would
    # print whatever the stream had retained by then.
    capture_sink: CaptureSink | None = dc.field(
        init=False, default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Build the accumulator this config's capture policy retains into."""
        object.__setattr__(
            self,
            "capture_sink",
            build_accumulator(
                self.capture,
                stream=self.stream.value,
                settings=_DecodeSettings.from_config(self),
            ),
        )

    @property
    def view_errors(self) -> str:
        """The error policy for a *view* of the child's bytes.

        Line observation and echo render a view rather than the run's capture,
        so in byte mode they replace undecodable input under
        :data:`cuprum._constants.OBSERVER_ERROR_POLICY` and cannot end a run
        whose bytes must reach the caller untouched. A text run keeps
        :attr:`errors`, so its views raise under a strict policy exactly as
        they did before byte mode existed. Deriving the choice here keeps the
        two view decoders from deciding it twice.
        """
        return OBSERVER_ERROR_POLICY if self.capture_bytes else self.errors


__all__ = [
    "_StreamConfig",
    "_resolve_stream_configs",
    "_resolve_stream_sink",
]
