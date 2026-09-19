"""Internal stream-handling utilities for subprocess I/O.

The pure-Python home for consuming a subprocess's stdout/stderr.
``_consume_stream`` and the shared ``_drain`` loop decode bytes, optionally tee
each chunk to a sink, capture the text, and emit decoded lines; the bounded echo
renderer those two call into lives in ``cuprum._stream_echo``, and the
line-boundary emitter that publishes decoded lines lives in
``cuprum._stream_line_consumer``. The writer side that pumps one pipeline
stage's stdout into the next stage's stdin lives in ``cuprum._streams_pump``
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

from cuprum._echo_truncation import (
    _EchoLineLimiter,
    _validate_bounded_echo_encoding,
)
from cuprum._stream_echo import (
    _echo_chunk,
    _echo_decoder,
    _flush_echo_decoder,
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
from cuprum.echo_events import EchoStream, RelayFallback
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
    # Which output stream this config drains, for bounded echo observability.
    # Defaults to stdout because every production call site names the stderr
    # config explicitly when it replaces the stdout one.
    stream: EchoStream = EchoStream.STDOUT
    # Run-owned observers, both optional and both unable to change what is
    # captured: ``activity`` reports that a non-empty chunk arrived, before any
    # decoding, truncation, or line callback could drop it, and ``mirror``
    # records where the echo sink ended up so a keepalive written later knows
    # whether it would land mid-line.
    activity: cabc.Callable[[], None] | None = None
    mirror: _MirrorCursor | None = None


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


@dc.dataclass(slots=True)
class _RelayDiagnostics:
    """Per-drain collector for handled echo-disablement records.

    One collector belongs to one command stream. Because the echo guard stops
    any later echo write after the first handled failure, a drain appends at
    most one :class:`~cuprum.echo_events.RelayFallback` here.
    """

    fallbacks: list[RelayFallback] = dc.field(default_factory=list)
    is_settled: bool = False

    def settle(self) -> None:
        """Publish the collected records for the owning command's result.

        Idempotent: the reconciliation paths run exactly once per drain, and a
        second call keeps whichever record list that call captured.
        """
        self.is_settled = True

    def snapshot(self) -> tuple[RelayFallback, ...]:
        """Return the collected records, or ``()`` before the drain settled.

        A drain that never settled — cancelled or abandoned during teardown —
        leaves its records unread: those diagnostics remain on the echo
        observation channel, so callers on a non-result path see ``()``.

        Returns
        -------
        tuple[RelayFallback, ...]
            The records collected before settlement, empty when the drain
            never settled or recorded nothing.
        """
        if not self.is_settled:
            return ()
        return tuple(self.fallbacks)


@dc.dataclass(slots=True)
class _EchoGuard:
    """Mutable holder tracking whether echo is disabled for one drain."""

    disabled: bool = False


@dc.dataclass(slots=True)
class _MirrorCursor:
    """Presentation-only record of whether a mirrored sink is mid-line.

    Shared with the idle heartbeat, which needs to know whether the last bytes
    echoed to the parent's stderr ended a line: a keepalive written now would
    otherwise become the tail of an unfinished mirrored line. Recording the
    position here, on the echo path, keeps the diagnostic free of any
    knowledge about the child's stream, and nothing in this class can affect
    what was captured.
    """

    is_mid_line: bool = False

    def note(self, chunk: bytes) -> None:
        """Record one written echo chunk; an empty chunk changes nothing."""
        if chunk:
            self.is_mid_line = not chunk.endswith(b"\n")


async def _consume_stream(
    stream: asyncio.StreamReader | None,
    config: _StreamConfig,
    *,
    on_line: _LineSink | None = None,
    relay_diagnostics: _RelayDiagnostics | None = None,
) -> str | None:
    """Read from a subprocess stream, teeing to sink when requested.

    ``relay_diagnostics`` defaults to a fresh collector, so a caller that does
    not own result diagnostics still gets a correct drain.

    Returns
    -------
    str | None
        The captured text, or ``None`` when capture is disabled.
    """
    if on_line is None:
        return await _consume_stream_without_lines(
            stream,
            config,
            relay_diagnostics=relay_diagnostics,
        )
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
) -> str | None:
    """Run the canonical read/echo/buffer loop over *stream*."""
    # This is the single source of truth for the consume mechanics shared by
    # :func:`_consume_stream_without_lines` and
    # :func:`_consume_stream_with_lines`: read in ``_READ_SIZE`` chunks, extend
    # the capture buffer when capturing, echo each chunk to the configured
    # sink when echoing, then hand the chunk to ``on_chunk`` for
    # variant-specific processing (for example incremental line decoding).
    # Fixes to the loop must be made here so the capture path and the
    # line-emitting path cannot drift.
    if config.echo_output and config.echo_max_line_bytes is not None:
        _validate_bounded_echo_encoding(config.encoding, config.errors)
    buffer = bytearray() if config.capture_output else None
    echo_decoder = _echo_decoder(config)
    echo_guard = _EchoGuard()
    echo_limiter = _EchoLineLimiter.from_config(
        echo_output=config.echo_output,
        echo_max_line_bytes=config.echo_max_line_bytes,
    )
    state = _DrainState(
        config,
        buffer,
        echo_decoder,
        on_chunk,
        echo_guard,
        relay_diagnostics or _RelayDiagnostics(),
        echo_limiter=echo_limiter,
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


def _finish_drain(
    state: _DrainState,
    measurement: _StreamOperationMeasurement | None,
    *,
    reached_eof: bool,
) -> str | None:
    """Complete one drain and return any captured text."""
    if not reached_eof:
        _complete_stream_operation(measurement, StreamOperationOutcome.CANCELLED)
        if state.buffer is None or _discard_on_cancel(state.config):
            raise asyncio.CancelledError
        _flush_echo_decoder(state)
        return state.buffer.decode(state.config.encoding, errors=state.config.errors)
    _flush_echo_decoder(state)
    captured = None
    if state.buffer is not None:
        captured = state.buffer.decode(
            state.config.encoding,
            errors=state.config.errors,
        )
    _complete_stream_operation(measurement, StreamOperationOutcome.EOF)
    return captured


def _discard_on_cancel(config: _StreamConfig) -> bool:
    """Whether cancellation must discard buffered bytes without decoding them."""
    return config.discard_on_cancel is not None and config.discard_on_cancel.is_set()


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
) -> str | None:
    """Read from a subprocess stream without emitting line callbacks."""
    if stream is None:
        return "" if config.capture_output else None
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
