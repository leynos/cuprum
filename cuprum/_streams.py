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
reader is supplied, so callers must not reuse the writer afterwards. It also
attempts to close the upstream reader when its bounded post-close drain times
out, which normally releases the producer of an early-closed stage from a pipe
that is never drained; the close is best-effort, so a missing transport, an
already-closing one, or a failed close may leave the producer unreleased.
Callers must not reuse that reader either.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._stream_config import _StreamConfig as _StreamConfig
from cuprum._stream_drain_finish import _CapturedDrain, _empty_capture, _finish_drain
from cuprum._stream_drain_loop import (
    _build_drain_state,
    _drain_chunks,
)
from cuprum._stream_drain_loop import (
    _DrainState as _DrainState,
)
from cuprum._stream_drain_state import _MirrorCursor, _RelayDiagnostics
from cuprum._stream_echo import _write_chunk
from cuprum._stream_line_boundaries import _split_complete_lines, _strip_line_ending
from cuprum._stream_line_consumer import (
    _consume_stream_with_lines,
    _emit_line,
    _LineConsumption,
)
from cuprum._stream_tap import _ChunkSink as _ChunkSink
from cuprum._stream_tap import _EofSink as _EofSink
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
    _start_stream_operation,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


# A per-line sink either returns ``None`` — it only fans out to observe hooks
# or a user callback — or an awaitable, which is how the ``lines()`` driver's
# bounded queue slows the read loop instead of dropping an event or retaining
# it without limit. :func:`_stream_line_consumer._emit_line` bridges the two.
type _LineSink = cabc.Callable[[str], cabc.Awaitable[None] | None]


async def _consume_stream(
    stream: asyncio.StreamReader | None,
    config: _StreamConfig,
    *,
    on_line: _LineSink | None = None,
    relay_diagnostics: _RelayDiagnostics | None = None,
) -> _CapturedDrain:
    """Read from a subprocess stream, teeing to sink when requested.

    A live byte tap travels on *config* rather than as an extra argument, so a
    byte run and a text run call this entry point identically. The tap's chunk
    observer runs after capture and echo and before any line decoding; its
    end-of-stream hook runs only for a genuine end.

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
            return await _finish_drain(state, measurement, reached_eof=True)
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
    return await _finish_drain(state, measurement, reached_eof=False)


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
    "_ChunkSink",
    "_EofSink",
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
