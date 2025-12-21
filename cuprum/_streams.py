"""Internal stream handling utilities for subprocess I/O operations."""

from __future__ import annotations

import codecs
import contextlib
import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import asyncio

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
    on_line: typ.Callable[[str], None] | None = None,
) -> str | None:
    """Read from a subprocess stream, teeing to sink when requested."""
    if on_line is None:
        return await _consume_stream_without_lines(stream, config)
    return await _consume_stream_with_lines(stream, config, on_line=on_line)


async def _consume_stream_without_lines(
    stream: asyncio.StreamReader | None,
    config: _StreamConfig,
) -> str | None:
    """Read from a subprocess stream without emitting line callbacks."""
    if stream is None:
        return "" if config.capture_output else None

    buffer = bytearray() if config.capture_output else None
    while True:
        chunk = await stream.read(_READ_SIZE)
        if not chunk:
            break
        if buffer is not None:
            buffer.extend(chunk)
        if config.echo_output:
            _write_chunk(
                config.sink,
                chunk,
                encoding=config.encoding,
                errors=config.errors,
            )

    if buffer is None:
        return None
    return buffer.decode(config.encoding, errors=config.errors)


async def _consume_stream_with_lines(
    stream: asyncio.StreamReader | None,
    config: _StreamConfig,
    *,
    on_line: typ.Callable[[str], None],
) -> str | None:
    """Read from a subprocess stream while emitting decoded output lines."""
    if stream is None:
        return "" if config.capture_output else None

    buffer = bytearray() if config.capture_output else None
    decoder_factory = codecs.getincrementaldecoder(config.encoding)
    decoder = decoder_factory(errors=config.errors)
    pending_text = ""

    while True:
        chunk = await stream.read(_READ_SIZE)
        if not chunk:
            break
        if buffer is not None:
            buffer.extend(chunk)
        if config.echo_output:
            _write_chunk(
                config.sink,
                chunk,
                encoding=config.encoding,
                errors=config.errors,
            )
        pending_text = _emit_completed_lines(
            pending_text + decoder.decode(chunk),
            on_line=on_line,
        )

    pending_text = _emit_completed_lines(
        pending_text + decoder.decode(b"", final=True),
        on_line=on_line,
    )
    if pending_text:
        on_line(_strip_line_ending(pending_text))

    if buffer is None:
        return None
    return buffer.decode(config.encoding, errors=config.errors)


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
    sink: typ.IO[str],
    chunk: bytes,
    *,
    encoding: str,
    errors: str,
) -> None:
    """Write a bytes chunk to a text sink synchronously, avoiding extra encoding.

    For stdio echo this blocking write is acceptable; future slow-sink handling
    can layer on a background writer if needed.
    """
    buffer = getattr(sink, "buffer", None)
    if buffer is not None:
        buffer.write(chunk)
        buffer.flush()
        return
    text = chunk.decode(encoding, errors=errors)
    sink.write(text)
    sink.flush()


def _emit_completed_lines(
    text: str,
    *,
    on_line: typ.Callable[[str], None],
) -> str:
    """Emit complete lines from text and return the remaining partial line."""
    lines = text.splitlines(keepends=True)
    if not lines:
        return text

    remainder = ""
    if not _ends_with_line_ending(lines[-1]):
        remainder = lines.pop()

    for line in lines:
        on_line(_strip_line_ending(line))

    return remainder


def _ends_with_line_ending(line: str) -> bool:
    return line.endswith("\n") or line.endswith("\r")


def _strip_line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1]
    return line
