"""Internal stream-handling utilities for subprocess I/O.

The pure-Python home for consuming a subprocess's stdout/stderr and pumping one
pipeline stage's stdout into the next stage's stdin. ``_consume_stream`` and the
shared ``_drain`` loop decode bytes, optionally tee each chunk to a sink, capture
the text, and emit decoded lines; ``_pump_stream``/``_relay_chunks`` copy chunks
between stages with ``writer.drain()`` backpressure, draining to EOF without a
writer and best-effort under a bounded timeout after an early close so upstream
stages never block. Used by the pipeline and single-command execution layers, it
mirrors the optional Rust backend ``cuprum._streams_rs``. Callers own any writer.
The synchronous decoding, echo-sink, and line-splitting helpers these loops drive
live in ``cuprum._stream_text``.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import enum
import logging
import typing as typ

from cuprum._stream_text import (
    _echo_decoder,
    _emit_completed_lines,
    _flush_echo_decoder,
    _incremental_decoder,
    _strip_line_ending,
    _write_chunk,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_READ_SIZE = 4096
_POST_CLOSE_DRAIN_TIMEOUT_S = 0.25
_LOGGER = logging.getLogger(__name__)


@dc.dataclass(frozen=True, slots=True)
class _StreamConfig:
    """Configuration for decoding and echoing a subprocess stream."""

    capture_output: bool
    echo_output: bool
    sink: typ.IO[str]
    encoding: str
    errors: str


@dc.dataclass(slots=True)
class _DrainProgress:
    """Track bytes discarded by a cancellable reader drain."""

    discarded_bytes: int = 0


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

    A capturing loop cancelled while parked in ``read()`` returns what it has
    buffered rather than propagating the cancellation, so the bytes the stream
    already produced survive teardown. A non-capturing loop has nothing to
    salvage and lets the cancellation through unchanged.
    """
    buffer = bytearray() if config.capture_output else None
    echo_decoder = _echo_decoder(config)
    while True:
        try:
            chunk = await stream.read(_READ_SIZE)
        except asyncio.CancelledError:
            if buffer is None:
                raise
            # Teardown cancels readers that are still short of EOF, and this
            # buffer is the run's only record of what the stream produced
            # before it arrived. Report it instead of discarding it. Nothing is
            # awaited past this point, so the task still settles on the turn
            # the cancellation asked for.
            break
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

    try:
        await _relay_chunks(reader, writer)
    finally:
        _LOGGER.debug("stream_writer_close_start")
        await _close_stream_writer(writer)


async def _relay_chunks(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
) -> None:
    """Copy chunks downstream; drain to EOF with no writer, bounded after a close."""
    if writer is None:
        await _drain_stream_reader(reader, _DrainProgress())
        return
    while True:
        chunk = await reader.read(_READ_SIZE)
        if not chunk:
            return
        if await _write_to_stream_writer(writer, chunk) is _WriteOutcome.CLOSED:
            break
    discarded_bytes = await _drain_stream_reader_bounded(reader)
    _LOGGER.warning(
        "stream_downstream_closed discarded_bytes=%s",
        discarded_bytes,
        extra={"cuprum_discarded_bytes": discarded_bytes},
    )


async def _drain_stream_reader(
    reader: asyncio.StreamReader,
    progress: _DrainProgress,
) -> int:
    """Consume the reader to EOF, discarding the data and returning byte count."""
    while chunk := await reader.read(_READ_SIZE):
        progress.discarded_bytes += len(chunk)
    return progress.discarded_bytes


async def _drain_stream_reader_bounded(reader: asyncio.StreamReader) -> int:
    """Best-effort drain after downstream closure without waiting forever."""
    progress = _DrainProgress()
    try:
        return await asyncio.wait_for(
            _drain_stream_reader(reader, progress),
            timeout=_POST_CLOSE_DRAIN_TIMEOUT_S,
        )
    except TimeoutError:
        _LOGGER.debug(
            "stream_reader_drain_timeout timeout_s=%s",
            _POST_CLOSE_DRAIN_TIMEOUT_S,
            extra={"cuprum_timeout_s": _POST_CLOSE_DRAIN_TIMEOUT_S},
        )
        return progress.discarded_bytes


async def _write_to_stream_writer(
    writer: asyncio.StreamWriter,
    chunk: bytes,
) -> _WriteOutcome:
    """Write a chunk; report whether the caller-owned downstream writer stays open."""
    try:
        writer.write(chunk)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        _LOGGER.warning(
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
    try:
        writer.write_eof()
    except (
        AttributeError,
        NotImplementedError,
        BrokenPipeError,
        ConnectionResetError,
    ) as exc:
        _log_suppressed_stream_close_error("write_eof", exc)
    try:
        writer.close()
    except (BrokenPipeError, ConnectionResetError) as exc:
        _log_suppressed_stream_close_error("close", exc)
        return
    wait_closed = getattr(writer, "wait_closed", None)
    if wait_closed is None:
        return
    try:
        await wait_closed()
    except (
        AttributeError,
        NotImplementedError,
        BrokenPipeError,
        ConnectionResetError,
    ) as exc:
        _log_suppressed_stream_close_error("wait_closed", exc)


def _log_suppressed_stream_close_error(operation: str, exc: BaseException) -> None:
    """Log a cleanup error that cannot safely be raised during pipe teardown."""
    _LOGGER.debug(
        "stream_writer_close_suppressed operation=%s error=%s",
        operation,
        type(exc).__name__,
        exc_info=(type(exc), exc, exc.__traceback__),
        extra={
            "cuprum_operation": operation,
            "cuprum_error_type": type(exc).__name__,
        },
    )


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
