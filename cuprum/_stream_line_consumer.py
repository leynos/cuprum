"""Incrementally decode a drained stream and publish completed lines.

Reached through :func:`cuprum._streams._consume_stream` whenever a line
observer is registered. Each decoded chunk is fed to a stateful
:class:`~cuprum._stream_line_boundaries._LineAccumulator`, which finds line
boundaries in the new chunk rather than rescanning text it has already seen,
and every complete line is published through :func:`_emit_line`, which awaits
a sink that answers with an awaitable: that await is what lets the ``lines()``
driver's bounded queue push back on a chatty child instead of queueing output
without bound. The pure splitting rules live in
``cuprum._stream_line_boundaries``.
"""

from __future__ import annotations

import codecs
import dataclasses as dc
import logging
import typing as typ

from cuprum._result_assembly import _require_bytes, _require_text
from cuprum._stream_drain_finish import _CapturedDrain, _empty_capture
from cuprum._stream_line_boundaries import _LineAccumulator

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc

    from cuprum._streams import _LineSink, _RelayDiagnostics, _StreamConfig


_LOGGER = logging.getLogger(__name__)
_LINE_BOUND_BUCKETS = (
    (1_024, "le_1k"),
    (4_096, "1k_4k"),
    (16_384, "4k_16k"),
    (65_536, "16k_64k"),
)


@dc.dataclass(frozen=True, slots=True)
class _LineConsumption:
    """Inputs for draining a stream while publishing decoded lines."""

    config: _StreamConfig
    on_line: _LineSink
    drain: cabc.Callable[..., cabc.Awaitable[_CapturedDrain]]
    relay_diagnostics: _RelayDiagnostics | None


@dc.dataclass(slots=True)
class _LinePublisher:
    """Decode and publish one observed stream's line batches."""

    consumption: _LineConsumption
    decoder: codecs.IncrementalDecoder
    accumulator: _LineAccumulator

    async def publish(self, lines: list[str]) -> None:
        """Send each completed line or fragment through the awaited sink."""
        for line in lines:
            await _emit_line(self.consumption.on_line, line)

    async def publish_decoded(self, text: str) -> None:
        """Publish one decoded batch and report its first overflow."""
        had_emitted_fragment = self.accumulator.has_emitted_fragment
        lines = self.accumulator.feed(text)
        if self.accumulator.has_emitted_fragment and not had_emitted_fragment:
            _report_line_fragmentation(self.consumption.config)
        await self.publish(lines)

    async def feed_decoder(self, chunk: bytes) -> None:
        """Decode one chunk and emit only lines whose boundary is complete."""
        await self.publish_decoded(self.decoder.decode(chunk))

    async def finish(self) -> None:
        """Flush decoder state and publish the final unterminated line."""
        await self.publish_decoded(self.decoder.decode(b"", final=True))
        trailing = self.accumulator.finish()
        if trailing is not None:
            await _emit_line(self.consumption.on_line, trailing)


async def _emit_line(sink: _LineSink, line: str) -> None:
    """Invoke a line sink, awaiting it when it applies backpressure."""
    outcome = sink(line)
    if outcome is not None:
        await outcome


async def _consume_stream_with_lines(
    stream: asyncio.StreamReader | None,
    consumption: _LineConsumption,
) -> _CapturedDrain:
    """Drain a stream while incrementally decoding and emitting complete lines.

    The decoder feeds the line sink only; the drain's capture accumulator is
    untouched, so a byte-exact config still yields the child's own bytes here
    and the widened payload is narrowed by the caller, not by this
    function. A stream that was never attached reports the empty capture in
    the run's own mode, matching
    :func:`cuprum._streams._consume_stream_without_lines`.

    Returns
    -------
    _CapturedDrain
        The captured payload, record, and spill handle from the drain.
    """
    if stream is None:
        return _empty_capture(consumption.config)

    publisher = _LinePublisher(
        consumption,
        _incremental_decoder(consumption.config),
        _LineAccumulator(consumption.config.max_line_chars),
    )

    captured = await consumption.drain(
        stream,
        consumption.config,
        on_chunk=publisher.feed_decoder,
        relay_diagnostics=consumption.relay_diagnostics,
    )
    await publisher.finish()
    if captured.payload is not None:
        if consumption.config.capture_bytes:
            _require_bytes(captured.payload, "line-observed stream")
        else:
            _require_text(captured.payload, "line-observed stream")
    return captured


def _report_line_fragmentation(config: _StreamConfig) -> None:
    """Log the first bounded-fragment outcome with fixed categories."""
    _LOGGER.debug(
        "Line observation emitted bounded fragments",
        extra={
            "cuprum_event": "line_observation_fragmented",
            "cuprum_stream": config.stream.value,
            "cuprum_outcome": "bounded_fragments_emitted",
            "cuprum_max_line_chars_bucket": _line_bound_bucket(config.max_line_chars),
        },
    )


def _line_bound_bucket(max_line_chars: int | None) -> str:
    """Map a configured line bound into a fixed diagnostic category."""
    if max_line_chars is None:
        return "unbounded"
    for limit, bucket in _LINE_BOUND_BUCKETS:
        if max_line_chars <= limit:
            return bucket
    return "gt_64k"


def _incremental_decoder(config: _StreamConfig) -> codecs.IncrementalDecoder:
    """Create a line-splitting decoder whose policy matches the run's mode.

    Line observation renders a *view* of the child's bytes; it is not where a
    run reports its output, and in a byte-exact run it must not be able to end
    the run and strand the bytes the caller asked for. The decode therefore
    replaces undecodable bytes in that mode, whatever policy the caller chose
    for the capture. Reading the caller's policy in byte mode instead would let
    an ambient observer change the outcome of a run it merely watches: a
    registered ``sh.observe()`` hook, or the line feeder the idle partition
    attaches, supplies a line sink the caller never asked for, and under
    ``errors="strict"`` the resulting :class:`UnicodeDecodeError` would escape
    from the drain's read loop and kill a ``run_bytes()`` that would otherwise
    have returned the child's bytes intact.

    A text run is deliberately not covered: there the view decodes under the
    caller's configured policy, in step with the capture, so ``run()`` and
    ``SafeCmd.lines()`` under ``errors="strict"`` still raise on undecodable
    input exactly as they did before byte mode existed. The byte-mode capture
    still reports undecodable input where it is read back: a byte-exact run
    hands back ``bytes`` untouched, and a text run raises when it decodes its
    buffer in :func:`cuprum._stream_drain_finish._captured_payload`.

    See :data:`cuprum._constants.OBSERVER_ERROR_POLICY` and
    :attr:`cuprum._streams._StreamConfig.view_errors` for the shared rationale.

    Returns
    -------
    codecs.IncrementalDecoder
        A decoder that replaces undecodable bytes in byte mode, and one that
        keeps the caller's policy otherwise.
    """
    decoder_factory = codecs.getincrementaldecoder(config.encoding)
    return decoder_factory(errors=config.view_errors)
