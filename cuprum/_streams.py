"""Internal stream handling utilities for subprocess I/O operations."""

from __future__ import annotations

import codecs
import contextlib
import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc

_READ_SIZE = 4096


@dc.dataclass(frozen=True, slots=True)
class _StreamConfig:
    """Configuration for decoding and echoing a subprocess stream."""

    capture_output: bool
    echo_output: bool
    sink: typ.IO[str]
    encoding: str
    errors: str


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
    """Run the canonical read/echo/buffer loop over *stream*.

    This is the single source of truth for the consume mechanics shared by
    :func:`_consume_stream_without_lines` and
    :func:`_consume_stream_with_lines`: read in ``_READ_SIZE`` chunks, extend
    the capture buffer when capturing, echo each chunk to the configured sink
    when echoing, then hand the chunk to ``on_chunk`` for variant-specific
    processing (for example incremental line decoding). Fixes to the loop must
    be made here so the capture path and the line-emitting path cannot drift.

    Returns the captured text decoded with the configured encoding/errors, or
    ``None`` when capture is disabled.
    """
    buffer = bytearray() if config.capture_output else None
    echo_decoder = _echo_decoder(config)
    while True:
        chunk = await stream.read(_READ_SIZE)
        if not chunk:
            break
        if buffer is not None:
            buffer.extend(chunk)
        if config.echo_output:
            _write_chunk(
                config,
                chunk,
                decoder=echo_decoder,
            )
        if on_chunk is not None:
            on_chunk(chunk)

    _flush_echo_decoder(config, echo_decoder)

    if buffer is None:
        return None
    return buffer.decode(config.encoding, errors=config.errors)


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


async def _pump_stream(
    reader: asyncio.StreamReader | None,
    writer: asyncio.StreamWriter | None,
) -> None:
    """Stream stdout into stdin with backpressure via ``drain``.

    When the downstream stdin closes early (for example because the next stage
    terminates), this helper continues draining stdout to avoid deadlocking
    upstream stages.
    """
    if reader is None:
        await _close_stream_writer(writer)
        return

    active_writer = writer
    while True:
        chunk = await reader.read(_READ_SIZE)
        if not chunk:
            break
        active_writer = await _write_to_stream_writer(active_writer, chunk)

    await _close_stream_writer(active_writer)


async def _write_to_stream_writer(
    writer: asyncio.StreamWriter | None,
    chunk: bytes,
) -> asyncio.StreamWriter | None:
    """Write a chunk to a writer, returning None when downstream closes."""
    if writer is None:
        return None
    try:
        writer.write(chunk)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        await _close_stream_writer(writer)
        return None
    return writer


async def _close_stream_writer(writer: asyncio.StreamWriter | None) -> None:
    """Close a writer, swallowing errors from already-closed pipes."""
    if writer is None:
        return
    with contextlib.suppress(
        AttributeError,
        NotImplementedError,
        BrokenPipeError,
        ConnectionResetError,
    ):
        writer.write_eof()
    try:
        writer.close()
    except (BrokenPipeError, ConnectionResetError):
        return
    wait_closed = getattr(writer, "wait_closed", None)
    if wait_closed is None:
        return
    with contextlib.suppress(
        AttributeError,
        NotImplementedError,
        BrokenPipeError,
        ConnectionResetError,
    ):
        await wait_closed()


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


def _flush_echo_decoder(
    config: _StreamConfig,
    decoder: codecs.IncrementalDecoder | None,
) -> None:
    """Flush a text-only echo decoder at end of stream."""
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
    return line.endswith("\n") or line.endswith("\r")


def _strip_line_ending(line: str) -> str:
    r"""Strip a single trailing ``\r\n``, ``\n``, or ``\r`` from ``line``."""
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1]
    return line
