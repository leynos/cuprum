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

if typ.TYPE_CHECKING:
    import collections.abc as cabc


_LOGGER = logging.getLogger("cuprum.stream")


@dc.dataclass(frozen=True, slots=True)
class _StreamConfig:
    """Configuration for decoding and echoing a subprocess stream."""

    capture_output: bool
    echo_output: bool
    sink: typ.IO[str]
    encoding: str
    errors: str
    discard_on_cancel: asyncio.Event | None = None
    # Which output stream this config drains, for bounded echo observability.
    # Defaults to stdout because every production call site names the stderr
    # config explicitly when it replaces the stdout one.
    stream: EchoStream = EchoStream.STDOUT


@dc.dataclass(frozen=True, slots=True)
class _DrainState:
    """State carried through one stream-drain loop."""

    config: _StreamConfig
    buffer: bytearray | None
    echo_decoder: codecs.IncrementalDecoder | None
    on_chunk: cabc.Callable[[bytes], None] | None
    # Payload of the frozen wrapper above: mutated in place once echo is
    # disabled, so the same drain shares the flag across its loop and final
    # decoder flush without rebinding this frozen field.
    echo_guard: _EchoGuard


@dc.dataclass(slots=True)
class _EchoGuard:
    """Mutable holder tracking whether echo is disabled for one drain."""

    disabled: bool = False


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
    echo_guard = _EchoGuard()
    state = _DrainState(config, buffer, echo_decoder, on_chunk, echo_guard)
    reached_eof = await _drain_chunks(stream, state)
    if not reached_eof:
        if buffer is None or _discard_on_cancel(config):
            raise asyncio.CancelledError
        _flush_echo_decoder(state)
        return buffer.decode(config.encoding, errors=config.errors)

    _flush_echo_decoder(state)

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
        final=True,
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


def _incremental_decoder(config: _StreamConfig) -> codecs.IncrementalDecoder:
    """Create an incremental decoder configured for a stream invocation."""
    decoder_factory = codecs.getincrementaldecoder(config.encoding)
    return decoder_factory(errors=config.errors)


def _echo_decoder(config: _StreamConfig) -> codecs.IncrementalDecoder | None:
    """Create the decoder needed by a text-only echo sink, if any."""
    if not config.echo_output or getattr(config.sink, "buffer", None) is not None:
        return None
    return _incremental_decoder(config)


def _echo_chunk(
    state: _DrainState,
    chunk: bytes,
    *,
    final: bool = False,
) -> None:
    """Echo one chunk to the sink, disabling echo if the sink cannot encode it.

    A text-only sink whose encoding cannot represent the subprocess output
    raises ``UnicodeEncodeError`` mid-drain. Letting that escape would abort
    stream consumption and lose the captured output, so the failure disables
    echoing for the rest of this drain (once per stream) while capture
    continues. Every other failure still propagates.
    """
    if state.echo_guard.disabled:
        return
    try:
        _write_chunk(state.config, chunk, decoder=state.echo_decoder, final=final)
    except UnicodeEncodeError as exc:
        state.echo_guard.disabled = True
        # The warning and the observation are two projections of the same
        # first-failure transition; neither retries after this point because
        # the guard above already disables every later echo write.
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


def _flush_echo_decoder(
    state: _DrainState,
) -> None:
    """Flush a text-only echo decoder at end of stream."""
    if state.echo_decoder is not None:
        _echo_chunk(state, b"", final=True)


def _emit_completed_lines(
    text: str,
    *,
    on_line: cabc.Callable[[str], None],
    final: bool = False,
) -> str:
    """Emit complete lines from text and return the remaining partial line."""
    lines, remainder = _split_complete_lines(text, final=final)

    for line in lines:
        on_line(line)

    return remainder


def _split_complete_lines(
    text: str,
    *,
    final: bool = True,
) -> tuple[list[str], str]:
    """Split text into completed lines and a trailing partial line.

    Parameters
    ----------
    text : str
        Text to split using Python's universal line boundary rules.
    final : bool
        Whether no more decoded text will arrive. A non-final trailing carriage
        return remains pending because it may prefix a following line feed.

    Returns
    -------
    tuple[list[str], str]
        Completed lines with one trailing line ending removed from each line,
        followed by the remaining partial line. The remainder is empty when
        ``text`` ends with a line ending or contains no partial line. A
        non-final trailing carriage return remains pending.
    """
    lines = text.splitlines(keepends=True)
    if not lines:
        return [], text

    remainder = ""
    if _should_hold_trailing_line(lines[-1], final=final):
        remainder = lines.pop()

    return [_strip_line_ending(line) for line in lines], remainder

def _should_hold_trailing_line(line: str, *, final: bool) -> bool:
    """Return whether a trailing line needs the next decoded chunk."""
    if not _ends_with_line_ending(line):
        return True
    return not final and line.endswith("\r")
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
