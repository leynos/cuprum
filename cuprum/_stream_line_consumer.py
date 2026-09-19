"""Incrementally decode a drained stream and publish completed lines.

Reached through :func:`cuprum._streams._consume_stream` whenever a line
observer is registered. Each decoded chunk is split on line boundaries and
every complete line is published through :func:`_emit_line`, which awaits a
sink that answers with an awaitable: that await is what lets the ``lines()``
driver's bounded queue push back on a chatty child instead of queueing output
without bound. The pure splitting rules live in
``cuprum._stream_line_boundaries``.
"""

from __future__ import annotations

import codecs
import dataclasses as dc
import typing as typ

from cuprum._stream_line_boundaries import _split_complete_lines, _strip_line_ending

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc

    from cuprum._streams import _LineSink, _RelayDiagnostics, _StreamConfig


@dc.dataclass(frozen=True, slots=True)
class _LineConsumption:
    """Inputs for draining a stream while publishing decoded lines."""

    config: _StreamConfig
    on_line: _LineSink
    drain: cabc.Callable[..., cabc.Awaitable[str | None]]
    relay_diagnostics: _RelayDiagnostics | None


async def _emit_line(sink: _LineSink, line: str) -> None:
    """Invoke a line sink, awaiting it when it applies backpressure."""
    outcome = sink(line)
    if outcome is not None:
        await outcome


async def _emit_completed_lines(
    text: str,
    *,
    on_line: _LineSink,
) -> str:
    """Emit complete lines from text and return the remaining partial line.

    Awaits each sink call so a sink that must apply backpressure to the
    producer holds the read loop instead of letting the lines queue without
    bound. See :func:`_emit_line`.

    Returns
    -------
    str
        The trailing partial line that carries no line ending yet, retained so
        the next chunk can complete it.
    """
    lines, remainder = _split_complete_lines(text, final=False)

    for line in lines:
        await _emit_line(on_line, line)

    return remainder


async def _consume_stream_with_lines(
    stream: asyncio.StreamReader | None,
    consumption: _LineConsumption,
) -> str | None:
    """Drain a stream while incrementally decoding and emitting complete lines."""
    if stream is None:
        return "" if consumption.config.capture_output else None

    decoder = _incremental_decoder(consumption.config)
    pending_text = ""

    async def feed_decoder(chunk: bytes) -> None:
        """Decode one chunk and emit only lines whose boundary is complete."""
        nonlocal pending_text
        pending_text = await _emit_completed_lines(
            pending_text + decoder.decode(chunk),
            on_line=consumption.on_line,
        )

    captured = await consumption.drain(
        stream,
        consumption.config,
        on_chunk=feed_decoder,
        relay_diagnostics=consumption.relay_diagnostics,
    )
    pending_text = await _emit_completed_lines(
        pending_text + decoder.decode(b"", final=True),
        on_line=consumption.on_line,
    )
    if pending_text:
        await _emit_line(consumption.on_line, _strip_line_ending(pending_text))
    return captured


def _incremental_decoder(config: _StreamConfig) -> codecs.IncrementalDecoder:
    """Create the configured incremental decoder."""
    decoder_factory = codecs.getincrementaldecoder(config.encoding)
    return decoder_factory(errors=config.errors)
