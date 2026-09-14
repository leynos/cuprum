"""The bounded echo renderer for a subprocess stream.

Mirroring a child's bytes to the parent is more than a write. Each logical line
is bounded before it reaches the sink (see ``cuprum._echo_truncation``), a sink
whose encoding cannot represent the child's bytes disables echo for that stream
alone, and the cursor recording whether the sink is mid-line advances only on a
write that actually landed. The drain loop in ``cuprum._streams`` reads the
bytes and owns the state this module renders.
"""

from __future__ import annotations

import codecs
import logging
import typing as typ

from cuprum._echo_truncation import _split_echo_segments
from cuprum.echo_events import EchoErrorCategory, EchoEvent
from cuprum.echo_observation import _emit_echo_event

if typ.TYPE_CHECKING:
    from cuprum._echo_truncation import _EchoLineLimiter
    from cuprum._streams import _DrainState, _StreamConfig


_LOGGER = logging.getLogger("cuprum.stream")


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
        mirror = state.config.mirror
        if mirror is not None:
            # Only a chunk that reached the sink moves the cursor: a write that
            # raised left the sink where it was.
            mirror.note(chunk)
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
        return False
    return True


def _flush_echo_decoder(
    state: _DrainState,
) -> None:
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


__all__ = [
    "_echo_chunk",
    "_echo_decoder",
    "_flush_echo_decoder",
    "_incremental_decoder",
    "_write_chunk",
]
