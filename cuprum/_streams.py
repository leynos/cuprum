"""Internal stream-handling utilities for subprocess I/O.

The pure-Python home for consuming a subprocess's stdout/stderr.
``_consume_stream`` and the shared ``_drain`` loop read raw chunks, optionally
tee each chunk to a sink, capture them, and emit decoded lines; the bounded echo
renderer those two call into lives in ``cuprum._stream_echo``, and the
line-boundary emitter that publishes decoded lines lives in
``cuprum._stream_line_consumer``. Capture and line emission are independent
channels: the drain's buffer keeps the child's own bytes, and only the line
feeder decodes, so one drain serves both modes. The writer side that pumps one
pipeline stage's stdout into the next stage's stdin lives in
``cuprum._streams_pump``
and is re-exported here (``_pump_stream``, ``_close_stream_writer``,
``_write_to_stream_writer``, ``_WriteOutcome``,
``_drain_stream_reader_bounded``) so importers of this module keep working
unchanged. Used by the pipeline and single-command execution layers, it mirrors
the optional Rust backend ``cuprum._streams_rs``. ``_pump_stream`` closes any
supplied writer once relay completes or when no reader is supplied, so callers
must not reuse the writer afterwards.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum._constants import OBSERVER_ERROR_POLICY
from cuprum._echo_truncation import (
    _EchoLineLimiter,
    _validate_bounded_echo_encoding,
)
from cuprum._stream_drain_finish import _finish_drain
from cuprum._stream_drain_state import _EchoGuard, _MirrorCursor, _RelayDiagnostics
from cuprum._stream_echo import (
    _echo_chunk,
    _echo_decoder,
    _write_chunk,
)
from cuprum._stream_line_boundaries import _split_complete_lines, _strip_line_ending
from cuprum._stream_line_consumer import (
    _consume_stream_with_lines,
    _emit_line,
    _LineConsumption,
)
from cuprum._streams_pump import (
    _POST_CLOSE_DRAIN_TIMEOUT_S,
    _READ_SIZE,
    _close_stream_writer,
    _drain_stream_reader_bounded,
    _pump_stream,
    _write_to_stream_writer,
    _WriteOutcome,
)
from cuprum.echo_events import BrokenPipePolicy, EchoStream
from cuprum.stream_events import StreamOperation, StreamOperationOutcome
from cuprum.stream_observation import (
    _complete_stream_operation,
    _record_stream_read,
    _start_stream_operation,
)

if typ.TYPE_CHECKING:
    import codecs
    import collections.abc as cabc

    from cuprum.stream_observation import _StreamOperationMeasurement


# A per-line sink either returns ``None`` — it only fans out to observe hooks
# or a user callback — or an awaitable, which is how the ``lines()`` driver's
# bounded queue slows the read loop instead of dropping an event or retaining
# it without limit. :func:`_stream_line_consumer._emit_line` bridges the two.
type _LineSink = cabc.Callable[[str], cabc.Awaitable[None] | None]
type _ChunkSink = cabc.Callable[[bytes], cabc.Awaitable[None] | None]


@dc.dataclass(frozen=True, slots=True)
class _StreamConfig:
    """Configuration for decoding and echoing a subprocess stream."""

    capture_output: bool
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


@dc.dataclass(frozen=True, slots=True)
class _DrainState:
    """State carried through one stream-drain loop."""

    config: _StreamConfig

    buffer: bytearray | None

    echo_decoder: codecs.IncrementalDecoder | None
    # Awaited between chunks, so a sink that must apply backpressure to the
    # producer can hold the read loop rather than buffer without bound. A sink
    # that answers with ``None`` is invoked and its value discarded.
    on_chunk: _ChunkSink | None
    # Payload of the frozen wrapper above: mutated in place once echo is
    # disabled, so the same drain shares the flag across its loop and final
    # decoder flush without rebinding this frozen field.

    echo_guard: _EchoGuard
    # Caller-owned result diagnostics: the collector a command hands to this
    # drain so a handled echo disablement can be surfaced on that command's
    # ``CommandResult.relay_fallbacks`` without touching the shared echo-hook
    # registry, which cannot attribute events to nested or concurrent runs.
    relay_diagnostics: _RelayDiagnostics
    echo_limiter: _EchoLineLimiter | None = None


async def _consume_stream(
    stream: asyncio.StreamReader | None,
    config: _StreamConfig,
    *,
    on_line: _LineSink | None = None,
    relay_diagnostics: _RelayDiagnostics | None = None,
) -> str | bytes | None:
    """Read from a subprocess stream, teeing to sink when requested.

    ``relay_diagnostics`` defaults to a fresh collector, so a caller that does
    not own result diagnostics still gets a correct drain.

    Returns
    -------
    str | bytes | None
        The captured payload — bytes when the config asks for byte-exact
        capture, decoded text otherwise — or ``None`` when capture is
        disabled.
    """
    if on_line is None:
        return await _consume_stream_without_lines(
            stream,
            config,
            relay_diagnostics=relay_diagnostics,
        )
    # Byte-exact capture and line observation compose, because the two facts
    # travel on separate channels: the drain extends its buffer with the raw
    # chunk, and only the line feeder decodes a copy to find boundaries. So a
    # byte-exact run still returns the child's own bytes while its observers
    # receive decoded lines, and the rejection of a *user* ``on_line`` in bytes
    # mode is a policy choice made by the entry points, not a limit of the
    # drain. See ``cuprum._bytes_run._validate_bytes_output``.
    return await _consume_stream_with_lines(
        stream,
        _LineConsumption(
            config=config,
            on_line=on_line,
            drain=_drain,
            relay_diagnostics=relay_diagnostics,
        ),
    )


async def _drain(
    stream: asyncio.StreamReader,
    config: _StreamConfig,
    *,
    on_chunk: _ChunkSink | None = None,
    relay_diagnostics: _RelayDiagnostics | None = None,
) -> str | bytes | None:
    """Run the canonical read/echo/buffer loop over *stream*."""
    # This is the single source of truth for the consume mechanics shared by
    # :func:`_consume_stream_without_lines` and
    # :func:`_consume_stream_with_lines`: read in ``_READ_SIZE`` chunks, extend
    # the capture buffer when capturing, echo each chunk to the configured
    # sink when echoing, then hand the chunk to ``on_chunk`` for
    # variant-specific processing (for example incremental line decoding).
    # Fixes to the loop must be made here so the capture path and the
    # line-emitting path cannot drift.
    state = _build_drain_state(
        config,
        on_chunk=on_chunk,
        relay_diagnostics=relay_diagnostics,
    )
    measurement = _start_stream_operation(StreamOperation.DRAIN)
    try:
        reached_eof = await _drain_chunks(
            stream,
            state,
            measurement=measurement,
        )
        if reached_eof:
            return _finish_drain(state, measurement, reached_eof=True)
    except asyncio.CancelledError:
        # A cancellation landing inside an awaited ``on_chunk`` cannot be
        # absorbed by the read loop the way a cancelled read is, so it
        # completes the measurement here instead of through ``_finish_drain``.
        # Either route emits exactly one cancellation outcome.
        _complete_stream_operation(measurement, StreamOperationOutcome.CANCELLED)
        raise
    except BaseException:
        _complete_stream_operation(measurement, StreamOperationOutcome.FAILED)
        raise
    return _finish_drain(state, measurement, reached_eof=False)


def _build_drain_state(
    config: _StreamConfig,
    *,
    on_chunk: _ChunkSink | None,
    relay_diagnostics: _RelayDiagnostics | None,
) -> _DrainState:
    """Assemble the mutable state one drain loop threads through its chunks.

    The bounded-echo guard is validated here rather than in the loop because a
    misconfigured encoding must fail before the first byte is read.

    Returns
    -------
    _DrainState
        Fresh state carrying the capture buffer, echo machinery, and the
        per-chunk and relay diagnostics the loop threads through.
    """
    if config.echo_output and config.echo_max_line_bytes is not None:
        _validate_bounded_echo_encoding(config.encoding, config.errors)
    return _DrainState(
        config,
        bytearray() if config.capture_output else None,
        _echo_decoder(config),
        on_chunk,
        _EchoGuard(),
        relay_diagnostics or _RelayDiagnostics(),
        echo_limiter=_EchoLineLimiter.from_config(
            echo_output=config.echo_output,
            echo_max_line_bytes=config.echo_max_line_bytes,
        ),
    )


async def _drain_chunks(
    stream: asyncio.StreamReader,
    state: _DrainState,
    *,
    measurement: _StreamOperationMeasurement | None,
) -> bool:
    """Consume chunks until EOF, updating the caller-owned capture buffer."""
    while True:
        try:
            chunk = await stream.read(state.config.read_size)
        except asyncio.CancelledError:
            return False
        _record_stream_read(measurement, chunk)
        if not chunk:
            return True
        await _deliver_chunk(state, chunk)


async def _deliver_chunk(state: _DrainState, chunk: bytes) -> None:
    """Fan one non-empty chunk out to capture, echo, and the chunk sink."""
    # Activity is reported here, on the raw read, so that every way a chunk
    # can go on to be dropped still counts: undecodable bytes, output with
    # no line ending yet, a disabled mirror, and text truncated past the
    # echo bound all mean the child is talking. A run with idle reporting
    # off has no observer and pays nothing for this.
    activity = state.config.activity
    if activity is not None:
        activity()
    if state.buffer is not None:
        state.buffer.extend(chunk)
    if state.config.echo_output:
        _echo_chunk(state, chunk)
    if state.on_chunk is not None:
        # Sync observers — the idle partition's line feeder, say — return
        # ``None`` and are simply invoked; an asynchronous sink is awaited
        # so it can apply backpressure to the producer.
        outcome = state.on_chunk(chunk)
        if outcome is not None:
            await outcome


async def _consume_stream_without_lines(
    stream: asyncio.StreamReader | None,
    config: _StreamConfig,
    *,
    relay_diagnostics: _RelayDiagnostics | None = None,
) -> str | bytes | None:
    """Read from a subprocess stream without emitting line callbacks.

    A stream that was never attached is reported as an empty capture rather
    than as absent output — the run did capture, and the child simply had no
    such pipe — so the empty value has to match the mode: ``b""`` for a
    byte-exact run, ``""`` for a text one.

    Returns
    -------
    str | bytes | None
        The captured payload, or ``None`` when this config is not capturing.
    """
    if stream is None:
        if not config.capture_output:
            return None
        return b"" if config.capture_bytes else ""
    return await _drain(
        stream,
        config,
        relay_diagnostics=relay_diagnostics,
    )


__all__ = [
    "_POST_CLOSE_DRAIN_TIMEOUT_S",
    "_READ_SIZE",
    "_MirrorCursor",
    "_RelayDiagnostics",
    "_StreamConfig",
    "_WriteOutcome",
    "_close_stream_writer",
    "_consume_stream",
    "_drain",
    "_drain_stream_reader_bounded",
    "_emit_line",
    "_pump_stream",
    "_split_complete_lines",
    "_strip_line_ending",
    "_write_chunk",
    "_write_to_stream_writer",
]
