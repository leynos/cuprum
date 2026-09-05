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
import typing as typ

from cuprum._echo_truncation import _EchoLineLimiter
from cuprum._streams_pump import (
    _POST_CLOSE_DRAIN_TIMEOUT_S,
    _READ_SIZE,
    _close_stream_writer,
    _drain_stream_reader_bounded,
    _pump_stream,
    _write_to_stream_writer,
    _WriteOutcome,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@dc.dataclass(frozen=True, slots=True)
class _StreamConfig:
    """Configuration for decoding and echoing a subprocess stream."""

    capture_output: bool
    echo_output: bool
    sink: typ.IO[str]
    encoding: str
    errors: str
    # Byte bound for each line mirrored to the echo sink; ``None`` keeps the
    # raw chunk-for-chunk echo. Bounded echoing protects consumers that stop
    # accepting a line past a size limit (GitHub Actions job logs end at a
    # 64 KiB line) while capture stays byte-for-byte complete.
    echo_max_line_bytes: int | None = None
    discard_on_cancel: asyncio.Event | None = None


@dc.dataclass(frozen=True, slots=True)
class _DrainState:
    """Mutable-free state carried through one stream-drain loop."""

    config: _StreamConfig
    buffer: bytearray | None
    echo_decoder: codecs.IncrementalDecoder | None
    on_chunk: cabc.Callable[[bytes], None] | None
    echo_limiter: _EchoLineLimiter | None = None


async def _consume_stream(
    stream: asyncio.StreamReader | None,
    config: _StreamConfig,
    *,
    on_line: cabc.Callable[[str], None] | None = None,
) -> str | None:
    """Read from a subprocess stream, teeing to sink when requested."""
    if on_line is None:
        return await _consume_stream_without_lines(stream, config)
    return await _consume_stream_with_lines(stream, config, on_line=on_line)


async def _drain(
    stream: asyncio.StreamReader,
    config: _StreamConfig,
    *,
    on_chunk: cabc.Callable[[bytes], None] | None = None,
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
    buffer = bytearray() if config.capture_output else None
    echo_decoder = _echo_decoder(config)
    echo_limiter = _EchoLineLimiter.from_config(
        echo_output=config.echo_output,
        echo_max_line_bytes=config.echo_max_line_bytes,
    )
    state = _DrainState(
        config,
        buffer,
        echo_decoder,
        on_chunk,
        echo_limiter=echo_limiter,
    )
    reached_eof = await _drain_chunks(stream, state)
    if not reached_eof:
        if buffer is None or _discard_on_cancel(config):
            raise asyncio.CancelledError
        _flush_echo_decoder(config, echo_decoder, state=state)
        return buffer.decode(config.encoding, errors=config.errors)

    _flush_echo_decoder(config, echo_decoder, state=state)

    if buffer is None:
        return None
    return buffer.decode(config.encoding, errors=config.errors)


def _discard_on_cancel(config: _StreamConfig) -> bool:
    """Whether cancellation must discard buffered bytes without decoding them."""
    return config.discard_on_cancel is not None and config.discard_on_cancel.is_set()


async def _drain_chunks(
    stream: asyncio.StreamReader,
    state: _DrainState,
) -> bool:
    """Consume chunks until EOF, updating the caller-owned capture buffer."""
    while True:
        try:
            chunk = await stream.read(_READ_SIZE)
        except asyncio.CancelledError:
            return False
        if not chunk:
            return True
        if state.buffer is not None:
            state.buffer.extend(chunk)
        if state.config.echo_output:
            _echo_chunk(state, chunk)
        if state.on_chunk is not None:
            state.on_chunk(chunk)


async def _consume_stream_without_lines(
    stream: asyncio.StreamReader | None,
    config: _StreamConfig,
) -> str | None:
    """Read from a subprocess stream without emitting line callbacks."""
    if stream is None:
        return "" if config.capture_output else None
    return await _drain(stream, config)


async def _consume_stream_with_lines(
    stream: asyncio.StreamReader | None,
    config: _StreamConfig,
    *,
    on_line: cabc.Callable[[str], None],
) -> str | None:
    """Read from a subprocess stream while emitting decoded output lines."""
    if stream is None:
        return "" if config.capture_output else None

    decoder = _incremental_decoder(config)
    pending_text = ""

    def feed_decoder(chunk: bytes) -> None:
        """Feed a chunk to the incremental decoder and emit complete lines."""
        nonlocal pending_text
        pending_text = _emit_completed_lines(
            pending_text + decoder.decode(chunk),
            on_line=on_line,
        )

    captured = await _drain(stream, config, on_chunk=feed_decoder)

    pending_text = _emit_completed_lines(
        pending_text + decoder.decode(b"", final=True),
        on_line=on_line,
    )
    if pending_text:
        on_line(_strip_line_ending(pending_text))

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


def _echo_chunk(state: _DrainState, chunk: bytes) -> None:
    """Echo *chunk* to the sink, honouring the per-line byte bound when set."""
    limiter = state.echo_limiter
    if limiter is None:
        _write_chunk(state.config, chunk, decoder=state.echo_decoder)
        return
    for body, ending in _split_echo_segments(chunk):
        kept = limiter.bound_line(body)
        if kept:
            _write_echo_bytes(state, kept)
        if ending is None:
            continue
        marker = limiter.finish_line(encoding=state.config.encoding)
        if marker is not None:
            _write_echo_bytes(state, marker)
        _write_echo_bytes(state, ending)


def _split_echo_segments(
    chunk: bytes,
) -> list[tuple[bytes, bytes | None]]:
    r"""Split *chunk* into per-line echo writes for the bounded echo path.

    Parameters
    ----------
    chunk : bytes
        Raw bytes just read from the child stream.

    Returns
    -------
    list[tuple[bytes, bytes | None]]
        ``(body, ending)`` pairs in stream order, where ``body`` excludes its
        ``\\n`` terminator and ``ending`` is the raw line ending (``\\n`` or
        ``\\r\\n``). ``ending`` is ``None`` for the trailing pair when the
        chunk ends mid-line; its bytes still reach the limiter so a partial
        truncated line stays bounded before EOF. Unterminated bytes are
        re-emitted whole from the next chunk, so the limiter's own counters
        are the only cross-chunk state.
    """
    segments: list[tuple[bytes, bytes | None]] = []
    start = 0
    data = chunk
    while True:
        end = data.find(b"\n", start)
        if end == -1:
            break
        body = data[start:end]
        if body.endswith(b"\r"):
            segments.append((body[:-1], b"\r\n"))
        else:
            segments.append((body, b"\n"))
        start = end + 1
    if start < len(data):
        segments.append((data[start:], None))
    return segments


def _write_echo_bytes(state: _DrainState, payload: bytes) -> None:
    """Write bounded echo bytes, decoding for text-only sinks."""
    _write_chunk(state.config, payload, decoder=state.echo_decoder)


def _incremental_decoder(config: _StreamConfig) -> codecs.IncrementalDecoder:
    """Create an incremental decoder configured for a stream invocation."""
    decoder_factory = codecs.getincrementaldecoder(config.encoding)
    return decoder_factory(errors=config.errors)


def _echo_decoder(config: _StreamConfig) -> codecs.IncrementalDecoder | None:
    """Create the decoder needed by a text-only echo sink, if any."""
    if not config.echo_output or getattr(config.sink, "buffer", None) is not None:
        return None
    return _incremental_decoder(config)


def _flush_echo_decoder(
    config: _StreamConfig,
    decoder: codecs.IncrementalDecoder | None,
    *,
    state: _DrainState | None = None,
) -> None:
    """Flush a text-only echo decoder at end of stream."""
    if state is not None and state.echo_limiter is not None:
        marker = state.echo_limiter.finish_line(encoding=config.encoding)
        if marker is not None:
            _write_echo_bytes(state, marker)
    if decoder is not None:
        _write_chunk(config, b"", decoder=decoder, final=True)


def _emit_completed_lines(
    text: str,
    *,
    on_line: cabc.Callable[[str], None],
) -> str:
    """Emit complete lines from text and return the remaining partial line."""
    lines, remainder = _split_complete_lines(text)

    for line in lines:
        on_line(line)

    return remainder


def _split_complete_lines(text: str) -> tuple[list[str], str]:
    """Split text into completed lines and a trailing partial line.

    Parameters
    ----------
    text : str
        Text to split using Python's universal line boundary rules.

    Returns
    -------
    tuple[list[str], str]
        Completed lines with one trailing line ending removed from each line,
        followed by the remaining partial line. The remainder is empty when
        ``text`` ends with a line ending or contains no partial line.
    """
    lines = text.splitlines(keepends=True)
    if not lines:
        return [], text

    remainder = ""
    if not _ends_with_line_ending(lines[-1]):
        remainder = lines.pop()

    return [_strip_line_ending(line) for line in lines], remainder


def _ends_with_line_ending(line: str) -> bool:
    """Return whether ``line`` ends with a newline or carriage return."""
    return line.endswith(("\n", "\r"))


def _strip_line_ending(line: str) -> str:
    r"""Strip a single trailing ``\r\n``, ``\n``, or ``\r`` from ``line``."""
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith(("\n", "\r")):
        return line[:-1]
    return line


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
