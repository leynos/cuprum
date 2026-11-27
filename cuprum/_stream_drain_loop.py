"""The read loop and per-chunk fan-out of one stream drain.

Split from ``cuprum._streams`` when the live byte tap and the end-of-stream
hook pushed that module past the repository's 400-line ceiling. This module
owns the machinery a drain runs *after* it is handed a stream: the state one
loop threads through its chunks, the read loop itself, and the single
capture-then-echo-then-sink fan-out that keeps those channels in one order.

``cuprum._streams`` keeps the entry points — ``_consume_stream``, ``_drain``,
and the no-line wrapper — and re-exports the names here so existing importers
and annotation sites keep working unchanged.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum._echo_truncation import (
    _EchoLineLimiter,
    _validate_bounded_echo_encoding,
)
from cuprum._stream_drain_state import _EchoGuard, _RelayDiagnostics
from cuprum._stream_echo import _echo_chunk, _echo_decoder
from cuprum.stream_observation import _record_stream_read

if typ.TYPE_CHECKING:
    import codecs

    from cuprum._capture_types import CaptureSink
    from cuprum._stream_config import _StreamConfig
    from cuprum._stream_tap import _ChunkSink, _EofSink
    from cuprum.stream_observation import _StreamOperationMeasurement


@dc.dataclass(frozen=True, slots=True)
class _DrainState:
    """State carried through one stream-drain loop."""

    config: _StreamConfig

    # Retains this stream's bytes under the config's policy, or is ``None``
    # when the run captures nothing. An accumulator rather than a bare
    # ``bytearray``, so a bounded or file-backed policy is applied by what the
    # loop feeds rather than by a branch the loop takes. Not named ``sink``:
    # ``_StreamConfig.sink`` is the echo destination, and the two are
    # deliberately independent.
    capture: CaptureSink | None

    echo_decoder: codecs.IncrementalDecoder | None
    # Awaited between chunks, so a sink that must apply backpressure to the
    # producer can hold the read loop rather than buffer without bound. A sink
    # that answers with ``None`` is invoked and its value discarded.
    on_chunk: _ChunkSink | None
    # Payload of the frozen wrapper above: mutated in place once echo is
    # disabled, so the same drain shares the flag across its loop and final
    # decoder flush without rebinding this frozen field.

    echo_guard: _EchoGuard
    # Caller-owned result diagnostics: the collector a command hands to this
    # drain so echo failures and delivery loss can appear in that command's
    # ``CommandResult.relay_fallbacks`` without touching the shared echo-hook
    # registry, which cannot attribute events to nested or concurrent runs.
    relay_diagnostics: _RelayDiagnostics
    echo_limiter: _EchoLineLimiter | None = None
    # Called exactly once when the read loop observes a genuine end-of-stream,
    # before the capture is finalized. It is *not* called for a cancelled read,
    # a failed chunk delivery, or a preserving line-stream finalization: those
    # are not ends of the child's stream, and a live reader must not claim one.
    on_eof: _EofSink | None = None


def _build_drain_state(
    config: _StreamConfig,
    *,
    on_chunk: _ChunkSink | None,
    relay_diagnostics: _RelayDiagnostics | None,
) -> _DrainState:
    """Assemble the mutable state one drain loop threads through its chunks.

    The bounded-echo guard is validated here rather than in the loop because a
    misconfigured encoding must fail before the first byte is read. The live
    end-of-stream hook comes from the config's tap, so a byte run and a text run
    differ by their configuration rather than by a drain argument.

    Returns
    -------
    _DrainState
        Fresh state carrying the capture accumulator, echo machinery, and the
        per-chunk and relay diagnostics the loop threads through.
    """
    if config.echo_output and config.echo_max_line_bytes is not None:
        _validate_bounded_echo_encoding(config.encoding, config.errors)
    return _DrainState(
        config,
        config.capture_sink,
        _echo_decoder(config),
        on_chunk,
        _EchoGuard(),
        relay_diagnostics or _RelayDiagnostics(),
        echo_limiter=_EchoLineLimiter.from_config(
            echo_output=config.echo_output,
            echo_max_line_bytes=config.echo_max_line_bytes,
        ),
        on_eof=None if config.tap is None else config.tap.on_eof,
    )


async def _drain_chunks(
    stream: asyncio.StreamReader,
    state: _DrainState,
    *,
    measurement: _StreamOperationMeasurement | None,
) -> bool:
    """Consume chunks until EOF, updating the caller-owned capture buffer.

    Returns
    -------
    bool
        ``True`` when the stream reached EOF, ``False`` when the loop stopped
        short — because a read was cancelled, or because a chunk's absorb was.
        Both stops finalize the prefix already captured; see
        :func:`_deliver_chunk`.
    """
    while True:
        try:
            chunk = await stream.read(state.config.read_size)
        except asyncio.CancelledError:
            return False
        _record_stream_read(measurement, chunk)
        if not chunk:
            # A genuine end-of-stream, observed before the capture is
            # finalized: a live observer must learn the stream ended while the
            # drain still owns a coherent accumulate. Cancellation and a failed
            # delivery return ``False`` without reaching here, so neither
            # claims an end the child never produced.
            if state.on_eof is not None:
                state.on_eof()
            return True
        if not await _deliver_chunk(state, chunk):
            return False


async def _deliver_chunk(state: _DrainState, chunk: bytes) -> bool:
    """Fan one non-empty chunk out to capture, echo, and the chunk sink.

    A file-backed capture makes ``absorb`` a real suspension point — it awaits
    a worker write once the stream has spilled — so a cancellation can land
    there, not only in the read. It is treated exactly as a cancelled read is:
    the stop is reported to the loop, which finalizes the prefix under the same
    rule. The write is shielded, so by the time the cancellation is re-raised
    the chunk has landed in full; discarding the capture at that point would
    lose bytes the file already holds.

    Only ``absorb``'s cancellation is absorbed here. One raised from
    ``on_chunk`` is a caller asking for the drain itself to stop, and it still
    propagates.

    Returns
    -------
    bool
        ``True`` when the chunk was delivered, ``False`` when a cancellation
        stopped the drain during capture.
    """
    _note_stream_activity(state)
    if not await _absorb_into_capture(state, chunk):
        return False
    await _fan_out_chunk(state, chunk)
    return True


def _note_stream_activity(state: _DrainState) -> None:
    """Report one raw read, before any consumer can drop part of it.

    Activity is reported here, on the raw read, so that every way a chunk can
    go on to be dropped still counts: undecodable bytes, output with no line
    ending yet, a disabled mirror, and text truncated past the echo bound all
    mean the child is talking. A run with idle reporting off has no observer
    and pays nothing for this.
    """
    activity = state.config.activity
    if activity is not None:
        activity()


async def _absorb_into_capture(state: _DrainState, chunk: bytes) -> bool:
    """Hand one chunk to the capture sink, reporting a cancelled absorb.

    Awaited even when the accumulator keeps its bytes on the heap: a
    file-backed capture writes to disk here, and the one protocol is what keeps
    this loop free of policy tests. See
    :class:`~cuprum._capture_types.CaptureSink`.

    Returns
    -------
    bool
        ``False`` when the absorb was cancelled and the drain must stop.
    """
    capture = state.capture
    if capture is None:
        return True
    try:
        await capture.absorb(chunk)
    except asyncio.CancelledError:
        return False
    return True


async def _fan_out_chunk(state: _DrainState, chunk: bytes) -> None:
    """Mirror one chunk and publish it to the drain's chunk sink.

    The live tap is invoked after echo and before the chunk sink, so a byte
    consumer and a line observer see the same chunk in the order capture and
    echo produced it. The chunk sink is invoked last, so a synchronous observer
    sees the mirror write that preceded it. An asynchronous sink is awaited
    here, which is what lets either apply backpressure to the producer.
    """
    if state.config.echo_output:
        _echo_chunk(state, chunk)
    tap = state.config.tap
    if tap is not None:
        outcome = tap.on_chunk(chunk)
        if outcome is not None:
            await outcome
    if state.on_chunk is None:
        return
    # Sync observers — the idle partition's line feeder, say — return ``None``
    # and are simply invoked rather than awaited.
    outcome = state.on_chunk(chunk)
    if outcome is not None:
        await outcome


__all__ = [
    "_DrainState",
    "_absorb_into_capture",
    "_build_drain_state",
    "_deliver_chunk",
    "_drain_chunks",
    "_fan_out_chunk",
    "_note_stream_activity",
]
