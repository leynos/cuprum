"""Echo one drained stream while preserving capture and bounded output policy.

This module owns the private write side of the Python stream drain: binary
passthrough, incremental decoding for text sinks, bounded-line mirroring, and
the privacy-safe transition when a text sink rejects a payload. ``_streams``
owns drain lifecycle and re-exports ``_write_chunk`` for existing private
consumers; new stream-drain code should continue to use that surface.
"""

from __future__ import annotations

import codecs
import logging
import typing as typ

from cuprum._echo_truncation import (
    _EchoLineLimiter,
    _split_echo_segments,
)
from cuprum.echo_events import EchoErrorCategory, EchoEvent, RelayFallback
from cuprum.echo_observation import _emit_echo_event

if typ.TYPE_CHECKING:
    from cuprum._streams import _StreamConfig


_LOGGER = logging.getLogger("cuprum.stream")


class _EchoGuard(typ.Protocol):
    """Mutable echo-disablement state held by a drain."""

    disabled: bool


class _RelayDiagnostics(typ.Protocol):
    """Sink for a drain's handled echo-disablement record."""

    fallbacks: list[RelayFallback]


class _EchoDrainState(typ.Protocol):
    """The echo-relevant subset of a stream drain's state."""

    @property
    def config(self) -> _StreamConfig:
        """The drain's immutable stream configuration."""

    @property
    def echo_decoder(self) -> codecs.IncrementalDecoder | None:
        """The optional text-sink decoder."""

    @property
    def echo_guard(self) -> _EchoGuard:
        """The mutable per-drain echo guard."""

    @property
    def relay_diagnostics(self) -> _RelayDiagnostics:
        """The caller-owned fallback collector."""

    @property
    def echo_limiter(self) -> _EchoLineLimiter | None:
        """The optional bounded-line echo limiter."""


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


def _echo_chunk(state: _EchoDrainState, chunk: bytes) -> None:
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
    state: _EchoDrainState,
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
    state: _EchoDrainState,
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
    state: _EchoDrainState,
    chunk: bytes,
    *,
    final: bool = False,
) -> bool:
    """Write one echo payload and report whether the sink accepted it."""
    if state.echo_guard.disabled:
        return False
    try:
        _write_chunk(state.config, chunk, decoder=state.echo_decoder, final=final)
    except UnicodeEncodeError:
        state.echo_guard.disabled = True
        state.relay_diagnostics.fallbacks.append(
            RelayFallback(
                stream=state.config.stream,
                error_category=EchoErrorCategory.UNICODE_ENCODE,
            ),
        )
        _emit_echo_event(
            EchoEvent(
                stream=state.config.stream,
                error_category=EchoErrorCategory.UNICODE_ENCODE,
            ),
        )
        _LOGGER.warning(
            "echo_disabled_stream_rejected_output",
            extra={
                "cuprum_operation": "echo_chunk",
                "cuprum_stream": str(state.config.stream),
                "cuprum_transition": "echo_disabled",
                "cuprum_error_category": EchoErrorCategory.UNICODE_ENCODE.value,
            },
        )
        return False
    return True


def _flush_echo_decoder(state: _EchoDrainState) -> None:
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
