"""The bounded echo renderer for a subprocess stream.

Mirroring a child's bytes to the parent is more than a write. Each logical line
is bounded before it reaches the sink (see ``cuprum._echo_truncation``), a sink
whose encoding cannot represent the child's bytes disables echo for that stream
alone, and the cursor recording whether the sink is mid-line advances only on a
write that actually landed. A disablement is also recorded on the drain's
caller-owned relay diagnostics, so the owning command can surface it as a
result record without going through the process-wide echo hook registry. The
drain loop in ``cuprum._streams`` reads the bytes and owns the state this
module renders.

A sink whose destination has closed reports ``BrokenPipeError``. That is the
second recoverable failure, but unlike an unencodable payload it is not always
the caller's to accept, so it is handled only when the stream config names
:data:`~cuprum.echo_events.BrokenPipePolicy.BEST_EFFORT`; the default
propagates it unchanged. Both recoveries share :func:`_disable_echo`, so the
guard flip and its three bounded projections cannot drift apart.
"""

from __future__ import annotations

import codecs
import logging
import typing as typ

from cuprum._echo_truncation import _split_echo_segments
from cuprum.echo_events import (
    BrokenPipePolicy,
    EchoErrorCategory,
    EchoEvent,
    RelayFallback,
)
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
    except UnicodeEncodeError:
        return _disable_echo(
            state,
            error_category=EchoErrorCategory.UNICODE_ENCODE,
        )
    except BrokenPipeError:
        # A broken pipe is the one sink failure whose meaning depends on the
        # caller's intent: a destination that went away is worth tolerating, a
        # genuinely unreachable device is not, and this clause cannot tell them
        # apart. So the policy decides, and the default keeps propagating.
        # ``BrokenPipeError`` is caught by name rather than as ``OSError``,
        # because widening it here would swallow every other device failure.
        if state.config.broken_pipe_policy is not BrokenPipePolicy.BEST_EFFORT:
            raise
        return _disable_echo(
            state,
            error_category=EchoErrorCategory.BROKEN_PIPE,
        )
    return True


def _disable_echo(state: _DrainState, *, error_category: EchoErrorCategory) -> bool:
    """Stop echoing for this drain and report the transition once.

    Every recoverable echo failure is handled identically — the guard stops
    later chunks and the final decoder flush from re-entering the failed write,
    and the three projections below carry the same bounded vocabulary — so the
    recovery lives here once rather than beside each ``except`` clause. Only
    the category differs, and the caller supplies it.

    The child's bytes and the sink's identity stay out of the log: the record
    names the transition, the stream, and the category, which is what a caller
    needs to react, and nothing a caller could not already see on its own
    result.

    Returns
    -------
    bool
        Always ``False``: nothing was accepted by the sink.
    """
    state.echo_guard.disabled = True
    # The first failure emits both projections; the guard prevents retries.
    state.relay_diagnostics.fallbacks.append(
        RelayFallback(
            stream=state.config.stream,
            error_category=error_category,
        ),
    )
    _emit_echo_event(
        EchoEvent(
            stream=state.config.stream,
            error_category=error_category,
        ),
    )
    _LOGGER.warning(
        "echo_disabled_stream_rejected_output",
        extra={
            "cuprum_operation": "echo_chunk",
            "cuprum_stream": str(state.config.stream),
            "cuprum_transition": "echo_disabled",
            "cuprum_error_category": error_category.value,
        },
    )
    return False


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


__all__ = [
    "_echo_chunk",
    "_echo_decoder",
    "_flush_echo_decoder",
    "_incremental_decoder",
    "_write_chunk",
]
