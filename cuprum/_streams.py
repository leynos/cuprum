"""Internal stream-handling utilities for subprocess I/O.

The pure-Python home for consuming a subprocess's stdout/stderr.
``_consume_stream`` and the shared ``_drain`` loop decode bytes, optionally tee
each chunk to a sink, capture the text, and emit decoded lines. The writer side
that pumps one pipeline stage's stdout into the next stage's stdin lives in
``cuprum._streams_pump`` and is re-exported here (``_pump_stream``,
``_close_stream_writer``, ``_write_to_stream_writer``, ``_WriteOutcome``,
``_drain_stream_reader_bounded``) so importers of this module keep working
unchanged. Used by the pipeline and single-command execution layers, it mirrors
the optional Rust backend ``cuprum._streams_rs``. ``_pump_stream`` closes any
supplied writer once relay completes or when no reader is supplied, so callers
must not reuse the writer afterwards.
"""

from __future__ import annotations

import asyncio
import codecs
import dataclasses as dc
import logging
import typing as typ

from cuprum._echo_truncation import (
    _EchoLineLimiter,
    _split_echo_segments,
    _validate_bounded_echo_encoding,
)
from cuprum._line_splitting import _split_complete_lines, _strip_line_ending
from cuprum._streams_pump import (
    _POST_CLOSE_DRAIN_TIMEOUT_S,
    _READ_SIZE,
    _close_stream_writer,
    _drain_stream_reader_bounded,
    _pump_stream,
    _write_to_stream_writer,
    _WriteOutcome,
)
from cuprum.echo_events import EchoErrorCategory, EchoEvent, EchoStream
from cuprum.echo_observation import _emit_echo_event
from cuprum.stream_events import StreamOperation, StreamOperationOutcome
from cuprum.stream_observation import (
    _complete_stream_operation,
    _record_stream_read,
    _start_stream_operation,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.stream_observation import _StreamOperationMeasurement


_LOGGER = logging.getLogger("cuprum.stream")

# A per-line sink either returns ``None`` — it only fans out to observe hooks
# or a user callback — or an awaitable, which is how the ``lines()`` driver's
# bounded queue slows the read loop instead of dropping an event or retaining
# it without limit. :func:`_emit_line` bridges the two.
type _LineSink = cabc.Callable[[str], cabc.Awaitable[None] | None]
type _ChunkSink = cabc.Callable[[bytes], cabc.Awaitable[None]]


async def _emit_line(sink: _LineSink, line: str) -> None:
    """Invoke a line sink, awaiting it when it applies backpressure."""
    outcome = sink(line)
    if outcome is not None:
        await outcome


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
    # Byte cap per echoed line; capture remains byte-for-byte complete.
    echo_max_line_bytes: int | None = None
    discard_on_cancel: asyncio.Event | None = None
    # Drained output stream for bounded-echo observability.
    stream: EchoStream = EchoStream.STDOUT


@dc.dataclass(frozen=True, slots=True)
class _DrainState:
    """State carried through one stream-drain loop."""

    config: _StreamConfig

    buffer: bytearray | None

    echo_decoder: codecs.IncrementalDecoder | None
    # Awaited between chunks, so a sink that must apply backpressure to the
    # producer can hold the read loop rather than buffer without bound.
    on_chunk: _ChunkSink | None
    # Payload of the frozen wrapper above: mutated in place once echo is
    # disabled, so the same drain shares the flag across its loop and final
    # decoder flush without rebinding this frozen field.

    echo_guard: _EchoGuard

    echo_limiter: _EchoLineLimiter | None = None


@dc.dataclass(slots=True)
class _EchoGuard:
    """Mutable holder tracking whether echo is disabled for one drain."""

    disabled: bool = False


async def _consume_stream(
    stream: asyncio.StreamReader | None,
    config: _StreamConfig,
    *,
    on_line: _LineSink | None = None,
    read_size: int = _READ_SIZE,
) -> str | None:
    """Read from a subprocess stream, teeing to sink when requested."""
    if on_line is None:
        return await _consume_stream_without_lines(stream, config, read_size=read_size)
    return await _consume_stream_with_lines(
        stream,
        config,
        on_line=on_line,
        read_size=read_size,
    )


async def _drain(
    stream: asyncio.StreamReader,
    config: _StreamConfig,
    *,
    on_chunk: _ChunkSink | None = None,
    read_size: int = _READ_SIZE,
) -> str | None:
    """Run the canonical read/echo/buffer loop over *stream*."""
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
        echo_limiter=echo_limiter,
    )
    measurement = _start_stream_operation(StreamOperation.DRAIN)
    try:
        reached_eof = await _drain_chunks(
            stream,
            state,
            read_size=read_size,
            measurement=measurement,
        )
        if reached_eof:
            return _finish_drain(state, measurement, reached_eof=True)
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
    read_size: int,
    measurement: _StreamOperationMeasurement | None,
) -> bool:
    """Consume chunks until EOF, updating the caller-owned capture buffer."""
    while True:
        try:
            chunk = await stream.read(read_size)
        except asyncio.CancelledError:
            return False
        _record_stream_read(measurement, chunk)
        if not chunk:
            return True
        if state.buffer is not None:
            state.buffer.extend(chunk)
        if state.config.echo_output:
            _echo_chunk(state, chunk)
        if state.on_chunk is not None:
            await state.on_chunk(chunk)


async def _consume_stream_without_lines(
    stream: asyncio.StreamReader | None,
    config: _StreamConfig,
    *,
    read_size: int,
) -> str | None:
    """Read from a subprocess stream without emitting line callbacks."""
    if stream is None:
        return "" if config.capture_output else None
    return await _drain(stream, config, read_size=read_size)

async def _consume_stream_with_lines(
    stream: asyncio.StreamReader | None,
    config: _StreamConfig,
    *,
    on_line: _LineSink,
    read_size: int,
) -> str | None:
    """Read from a subprocess stream while emitting decoded output lines.

    Each emitted line is awaited through :func:`_emit_line`, so a sink that
    needs to wait for its consumer holds the read loop instead of queueing
    output without bound — an ``_READ_SIZE`` chunk can carry thousands of lines,
    so the bound cannot be enforced between reads alone.

    Returns
    -------
    str | None
        The captured text when the config captures output, and ``None`` when it
        does not. A ``stream`` of ``None`` — an unobserved pipe — captures the
        empty string in that case, matching the capture of a child that wrote
        nothing.
    """
    if stream is None:
        return "" if config.capture_output else None

    decoder = _incremental_decoder(config)
    pending_text = ""

    async def feed_decoder(chunk: bytes) -> None:
        """Feed a chunk to the incremental decoder and emit complete lines."""
        nonlocal pending_text
        pending_text = await _emit_completed_lines(
            pending_text + decoder.decode(chunk),
            on_line=on_line,
        )

    captured = await _drain(
        stream,
        config,
        on_chunk=feed_decoder,
        read_size=read_size,
    )

    pending_text = await _emit_completed_lines(
        pending_text + decoder.decode(b"", final=True),
        on_line=on_line,
    )
    if pending_text:
        await _emit_line(on_line, _strip_line_ending(pending_text))

    return captured


def _write_chunk(
    config: _StreamConfig,
    chunk: bytes,
    *,
    decoder: codecs.IncrementalDecoder | None = None,
    final: bool = False,
) -> None:
    """Write a bytes chunk to a sink synchronously, avoiding extra encoding.

    For stdio echo this blocking write is acceptable; future slow-sink handling
    can layer on a background writer if needed.
    """
    buffer = getattr(config.sink, "buffer", None)
    if buffer is not None:
        buffer.write(chunk)
        buffer.flush()
        return
    text = (
        chunk.decode(config.encoding, errors=config.errors)
        if decoder is None
        else decoder.decode(chunk, final=final)
    )
    if text:
        config.sink.write(text)
    config.sink.flush()


def _incremental_decoder(config: _StreamConfig) -> codecs.IncrementalDecoder:
    """Create an incremental decoder configured for a stream invocation."""
    decoder_factory = codecs.getincrementaldecoder(config.encoding)
    return decoder_factory(errors=config.errors)


def _echo_decoder(config: _StreamConfig) -> codecs.IncrementalDecoder | None:
    """Create the decoder needed by a text-only echo sink, if any."""
    if not config.echo_output or getattr(config.sink, "buffer", None) is not None:
        return None
    return _incremental_decoder(config)


def _echo_chunk(state: _DrainState, chunk: bytes) -> None:
    """Echo *chunk* to the sink, honouring the per-line byte bound when set."""
    if state.echo_guard.disabled:
        return
    limiter = state.echo_limiter
    if limiter is None:
        _echo_write(state, chunk)
        return
    data = _prepend_pending_carriage_return(limiter, chunk)
    for body, ending in _split_echo_segments(data):
        _echo_bounded_segment(state, limiter, body, ending)


def _prepend_pending_carriage_return(
    limiter: _EchoLineLimiter,
    chunk: bytes,
) -> bytes:
    """Hold a chunk-final CR until its line-ending role is known."""
    prefix = b"\r" if limiter.has_pending_carriage_return else b""
    limiter.has_pending_carriage_return = False
    data = prefix + chunk
    if data.endswith(b"\r"):
        limiter.has_pending_carriage_return = True
        return data[:-1]
    return data


def _echo_bounded_segment(
    state: _DrainState,
    limiter: _EchoLineLimiter,
    body: bytes,
    ending: bytes | None,
) -> None:
    """Accumulate one segment and mirror its completed line within the bound."""
    limiter.bound_line(body)
    if ending is None:
        return
    _write_finished_echo_line(state, limiter, ending)


def _write_finished_echo_line(
    state: _DrainState,
    limiter: _EchoLineLimiter,
    ending: bytes,
) -> None:
    """Write one finalized bounded echo line and observe a successful trim."""
    finished = limiter.finish_line(
        ending=ending,
        encoding=state.config.encoding,
        errors=state.config.errors,
        is_text_sink=state.echo_decoder is not None,
    )
    was_written = _echo_write(state, finished.payload)
    if was_written and finished.dropped_bytes:
        _emit_echo_event(
            EchoEvent(
                stream=state.config.stream,
                error_category=EchoErrorCategory.TRUNCATED,
                dropped_bytes=finished.dropped_bytes,
            ),
        )


def _echo_write(
    state: _DrainState,
    chunk: bytes,
    *,
    final: bool = False,
) -> bool:
    """Write one echo payload and report whether the sink accepted it."""
    if state.echo_guard.disabled:
        return False
    try:
        _write_chunk(state.config, chunk, decoder=state.echo_decoder, final=final)
    except UnicodeEncodeError as exc:
        state.echo_guard.disabled = True
        # The first failure emits both projections; the guard prevents retries.
        _emit_echo_event(
            EchoEvent(
                stream=state.config.stream,
                error_category=EchoErrorCategory.UNICODE_ENCODE,
            ),
        )
        _LOGGER.warning(
            "echo_disabled encoding=%s error=%s",
            state.config.encoding,
            type(exc).__name__,
            exc_info=exc,
            extra={
                "cuprum_encoding": state.config.encoding,
                "cuprum_sink_type": type(state.config.sink).__name__,
                "cuprum_error_type": type(exc).__name__,
            },
        )
        return False
    return True


def _flush_echo_decoder(state: _DrainState) -> None:
    """Flush a text-only echo decoder at end of stream."""
    limiter = state.echo_limiter
    if limiter is not None:
        if limiter.has_pending_carriage_return:
            limiter.bound_line(b"\r")
            limiter.has_pending_carriage_return = False
        if limiter.has_line_bytes:
            _write_finished_echo_line(state, limiter, b"")
    if state.echo_decoder is not None:
        _echo_write(state, b"", final=True)

async def _emit_completed_lines(
    text: str,
    *,
    on_line: _LineSink,
) -> str:
    """Emit complete lines from text and return the remaining partial line.

    The pure splitting rules live in ``cuprum._line_splitting``; this is the
    drain-side emitter, which awaits each sink call so a sink that must apply
    backpressure to the producer holds the read loop instead of letting the
    lines queue without bound. See :func:`_emit_line`.
    """
    lines, remainder = _split_complete_lines(text)

    for line in lines:
        await _emit_line(on_line, line)

    return remainder


__all__ = [
    "_POST_CLOSE_DRAIN_TIMEOUT_S",
    "_READ_SIZE",
    "_StreamConfig",
    "_WriteOutcome",
    "_close_stream_writer",
    "_consume_stream",
    "_drain",
    "_drain_stream_reader_bounded",
    "_pump_stream",
    "_split_complete_lines",
    "_strip_line_ending",
    "_write_chunk",
    "_write_to_stream_writer",
]
