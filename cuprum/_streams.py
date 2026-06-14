"""Internal stream handling utilities for subprocess I/O operations."""

from __future__ import annotations

import codecs
import contextlib
import dataclasses as dc
import enum
import logging
import typing as typ

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc

_READ_SIZE = 4096
_LOGGER = logging.getLogger("cuprum.streams")


class _WriteOutcome(enum.Enum):
    """Whether a downstream writer is still accepting data after a write.

    Used by :func:`_write_to_stream_writer` to report broken-pipe state as a
    value rather than overloading a ``StreamWriter | None`` return, so the
    caller retains ownership of the writer and decides when to close it.

    Members
    -------
    OPEN
        The write succeeded and the downstream writer remains open.
    CLOSED
        The downstream closed early (broken pipe); stop writing to it.
    """

    OPEN = "open"
    CLOSED = "closed"


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
    on_line: cabc.Callable[[str], None],
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

    try:
        await _relay_chunks(reader, writer)
    finally:
        _LOGGER.debug("stream_writer_close_start")
        await _close_stream_writer(writer)


async def _relay_chunks(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
) -> None:
    """Copy chunks downstream until EOF, draining after an early close.

    When the writer is absent or reports :attr:`_WriteOutcome.CLOSED`, the
    reader is still consumed to EOF so upstream stages do not block on a
    full pipe.
    """
    has_downstream_closed = False
    while writer is not None:
        chunk = await reader.read(_READ_SIZE)
        if not chunk:
            return
        if await _write_to_stream_writer(writer, chunk) is _WriteOutcome.CLOSED:
            has_downstream_closed = True
            break
    discarded_bytes = await _drain_stream_reader(reader)
    if has_downstream_closed:
        _LOGGER.debug(
            "stream_downstream_closed discarded_bytes=%s",
            discarded_bytes,
            extra={"cuprum_discarded_bytes": discarded_bytes},
        )


async def _drain_stream_reader(reader: asyncio.StreamReader) -> int:
    """Consume the reader to EOF, discarding the data and returning byte count."""
    discarded_bytes = 0
    while chunk := await reader.read(_READ_SIZE):
        discarded_bytes += len(chunk)
    return discarded_bytes


async def _write_to_stream_writer(
    writer: asyncio.StreamWriter,
    chunk: bytes,
) -> _WriteOutcome:
    """Write a chunk and report whether the downstream writer stays open.

    The writer is owned by the caller and is intentionally *not* closed here;
    a broken pipe is reported as :attr:`_WriteOutcome.CLOSED` so the caller can
    stop writing while continuing to drain upstream and close the writer once
    at the end.
    """
    try:
        writer.write(chunk)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        _LOGGER.debug(
            "stream_write_closed bytes=%s",
            len(chunk),
            extra={"cuprum_attempted_bytes": len(chunk)},
        )
        return _WriteOutcome.CLOSED
    return _WriteOutcome.OPEN


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
