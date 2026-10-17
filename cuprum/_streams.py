"""Internal stream-handling utilities for subprocess I/O.

The pure-Python home for consuming a subprocess's stdout/stderr.
``_consume_stream`` and the shared ``_drain`` loop read raw chunks, optionally
tee each chunk to a sink, capture them, and emit decoded lines; the bounded echo
renderer those two call into lives in ``cuprum._stream_echo``, and the
line-boundary emitter that publishes decoded lines lives in
``cuprum._stream_line_consumer``. What a drain is *handed* — the capture
policy, echo destination, and decode settings for one stream — lives in
``cuprum._stream_config``. Capture and line emission are independent
channels: the drain's buffer keeps the child's own bytes, and only the line
feeder decodes, so one drain serves both modes. The writer side that pumps one
pipeline stage's stdout into the next stage's stdin lives in
``cuprum._streams_pump``
and is re-exported here (``_pump_stream``, ``_close_stream_writer``,
``_write_to_stream_writer``, ``_WriteOutcome``,
``_drain_stream_reader_bounded``) so importers of this module keep working
unchanged, as is ``_StreamConfig``. Used by the pipeline and single-command
execution layers, it mirrors the optional Rust backend ``cuprum._streams_rs``.
``_pump_stream`` closes any supplied writer once relay completes or when no
reader is supplied, so callers must not reuse the writer afterwards.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum._echo_truncation import (
    _EchoLineLimiter,
    _validate_bounded_echo_encoding,
)
from cuprum._stream_config import _StreamConfig as _StreamConfig
from cuprum._stream_drain_finish import _CapturedDrain, _empty_capture, _finish_drain
from cuprum._stream_drain_state import _EchoGuard, _MirrorCursor, _RelayDiagnostics
from cuprum._stream_echo import (
    _echo_chunk,
    _echo_decoder,
    _write_chunk,
)
from cuprum._stream_line_boundaries import _split_complete_lines, _strip_line_ending
from cuprum._stream_line_consumer import (
    _consume_stream_with_lines,
    _emit_line,
    _LineConsumption,
)
from cuprum._streams_pump import (
    _POST_CLOSE_DRAIN_TIMEOUT_S,
    _READ_SIZE,
    _close_stream_writer,
    _drain_stream_reader_bounded,
    _pump_stream,
    _write_to_stream_writer,
    _WriteOutcome,
)
from cuprum.stream_events import StreamOperation, StreamOperationOutcome
from cuprum.stream_observation import (
    _complete_stream_operation,
    _record_stream_read,
    _start_stream_operation,
)

if typ.TYPE_CHECKING:
    import codecs
    import collections.abc as cabc

    from cuprum._capture_types import CaptureSink
    from cuprum.stream_observation import _StreamOperationMeasurement


# A per-line sink either returns ``None`` — it only fans out to observe hooks
# or a user callback — or an awaitable, which is how the ``lines()`` driver's
# bounded queue slows the read loop instead of dropping an event or retaining
# it without limit. :func:`_stream_line_consumer._emit_line` bridges the two.
type _LineSink = cabc.Callable[[str], cabc.Awaitable[None] | None]
type _ChunkSink = cabc.Callable[[bytes], cabc.Awaitable[None] | None]


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
    # drain so a handled echo disablement can be surfaced on that command's
    # ``CommandResult.relay_fallbacks`` without touching the shared echo-hook
    # registry, which cannot attribute events to nested or concurrent runs.
    relay_diagnostics: _RelayDiagnostics
    echo_limiter: _EchoLineLimiter | None = None


async def _consume_stream(
    stream: asyncio.StreamReader | None,
    config: _StreamConfig,
    *,
    on_line: _LineSink | None = None,
    relay_diagnostics: _RelayDiagnostics | None = None,
) -> _CapturedDrain:
    """Read from a subprocess stream, teeing to sink when requested.

    ``relay_diagnostics`` defaults to a fresh collector, so a caller that does
    not own result diagnostics still gets a correct drain.

    Returns
    -------
    _CapturedDrain
        The captured payload — bytes when the config asks for byte-exact
        capture, decoded text otherwise — with the record describing how the
        policy retained it. Both are ``None`` when capture is disabled.
    """
    if on_line is None:
        return await _consume_stream_without_lines(
            stream,
            config,
            relay_diagnostics=relay_diagnostics,
        )
    # Byte-exact capture and line observation compose, because the two facts
    # travel on separate channels: the drain extends its buffer with the raw
    # chunk, and only the line feeder decodes a copy to find boundaries. So a
    # byte-exact run still returns the child's own bytes while its observers
    # receive decoded lines, and the rejection of a *user* ``on_line`` in bytes
    # mode is a policy choice made by the entry points, not a limit of the
    # drain. See ``cuprum._bytes_run._validate_bytes_output``.
    return await _consume_stream_with_lines(
        stream,
        _LineConsumption(
            config=config,
            on_line=on_line,
            drain=_drain,
            relay_diagnostics=relay_diagnostics,
        ),
    )


async def _drain(
    stream: asyncio.StreamReader,
    config: _StreamConfig,
    *,
    on_chunk: _ChunkSink | None = None,
    relay_diagnostics: _RelayDiagnostics | None = None,
) -> _CapturedDrain:
    """Run the canonical read/echo/buffer loop over *stream*."""
    # This is the single source of truth for the consume mechanics shared by
    # :func:`_consume_stream_without_lines` and
    # :func:`_consume_stream_with_lines`: read in ``_READ_SIZE`` chunks, extend
    # the capture buffer when capturing, echo each chunk to the configured
    # sink when echoing, then hand the chunk to ``on_chunk`` for
    # variant-specific processing (for example incremental line decoding).
    # Fixes to the loop must be made here so the capture path and the
    # line-emitting path cannot drift.
    state = _build_drain_state(
        config,
        on_chunk=on_chunk,
        relay_diagnostics=relay_diagnostics,
    )
    measurement = _start_stream_operation(StreamOperation.DRAIN)
    try:
        reached_eof = await _drain_chunks(
            stream,
            state,
            measurement=measurement,
        )
        if reached_eof:
            return _finish_drain(state, measurement, reached_eof=True)
    except asyncio.CancelledError:
        # A cancellation landing inside an awaited ``on_chunk`` cannot be
        # absorbed by the read loop the way a cancelled read is, so it
        # completes the measurement here instead of through ``_finish_drain``.
        # Either route emits exactly one cancellation outcome.
        _complete_stream_operation(measurement, StreamOperationOutcome.CANCELLED)
        raise
    except BaseException:
        _complete_stream_operation(measurement, StreamOperationOutcome.FAILED)
        raise
    return _finish_drain(state, measurement, reached_eof=False)


def _build_drain_state(
    config: _StreamConfig,
    *,
    on_chunk: _ChunkSink | None,
    relay_diagnostics: _RelayDiagnostics | None,
) -> _DrainState:
    """Assemble the mutable state one drain loop threads through its chunks.

    The bounded-echo guard is validated here rather than in the loop because a
    misconfigured encoding must fail before the first byte is read.

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
    )


async def _drain_chunks(
    stream: asyncio.StreamReader,
    state: _DrainState,
    *,
    measurement: _StreamOperationMeasurement | None,
) -> bool:
    """Consume chunks until EOF, updating the caller-owned capture buffer."""
    while True:
        try:
            chunk = await stream.read(state.config.read_size)
        except asyncio.CancelledError:
            return False
        _record_stream_read(measurement, chunk)
        if not chunk:
            return True
        await _deliver_chunk(state, chunk)


async def _deliver_chunk(state: _DrainState, chunk: bytes) -> None:
    """Fan one non-empty chunk out to capture, echo, and the chunk sink."""
    # Activity is reported here, on the raw read, so that every way a chunk
    # can go on to be dropped still counts: undecodable bytes, output with
    # no line ending yet, a disabled mirror, and text truncated past the
    # echo bound all mean the child is talking. A run with idle reporting
    # off has no observer and pays nothing for this.
    activity = state.config.activity
    if activity is not None:
        activity()
    capture = state.capture
    if capture is not None:
        capture.absorb(chunk)
    if state.config.echo_output:
        _echo_chunk(state, chunk)
    if state.on_chunk is not None:
        # Sync observers — the idle partition's line feeder, say — return
        # ``None`` and are simply invoked; an asynchronous sink is awaited
        # so it can apply backpressure to the producer.
        outcome = state.on_chunk(chunk)
        if outcome is not None:
            await outcome


async def _consume_stream_without_lines(
    stream: asyncio.StreamReader | None,
    config: _StreamConfig,
    *,
    relay_diagnostics: _RelayDiagnostics | None = None,
) -> _CapturedDrain:
    """Read from a subprocess stream without emitting line callbacks.

    A stream that was never attached is reported as an empty capture rather
    than as absent output — the run did capture, and the child simply had no
    such pipe — so the empty value has to match the mode: ``b""`` for a
    byte-exact run, ``""`` for a text one.

    Both exits answer with the same shape. A branch that reported a bare empty
    payload here, where a real drain reports a payload *and* a record, would be
    a drain whose type depended on whether the child happened to have a pipe —
    a difference no caller could see coming, and the one the result builder
    would report as an execution-invariant error.

    Returns
    -------
    _CapturedDrain
        The captured payload — bytes when the config asks for byte-exact
        capture, decoded text otherwise — with the record describing how the
        policy retained it. All three fields are ``None`` when capture is
        disabled.
    """
    if stream is None:
        return _empty_capture(config)
    return await _drain(
        stream,
        config,
        relay_diagnostics=relay_diagnostics,
    )


__all__ = [
    "_POST_CLOSE_DRAIN_TIMEOUT_S",
    "_READ_SIZE",
    "_MirrorCursor",
    "_RelayDiagnostics",
    "_StreamConfig",
    "_WriteOutcome",
    "_close_stream_writer",
    "_consume_stream",
    "_drain",
    "_drain_stream_reader_bounded",
    "_emit_line",
    "_pump_stream",
    "_split_complete_lines",
    "_strip_line_ending",
    "_write_chunk",
    "_write_to_stream_writer",
]
