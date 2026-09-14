"""Incrementally decode a drained stream and publish completed lines."""

from __future__ import annotations

import codecs
import dataclasses as dc
import typing as typ

from cuprum._stream_line_boundaries import _emit_completed_lines, _strip_line_ending

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc

    from cuprum._streams import _StreamConfig


@dc.dataclass(frozen=True, slots=True)
class _LineConsumption:
    """Inputs for draining a stream while publishing decoded lines."""

    config: _StreamConfig
    on_line: cabc.Callable[[str], None]
    read_size: int
    drain: cabc.Callable[..., cabc.Awaitable[str | None]]


async def _consume_stream_with_lines(
    stream: asyncio.StreamReader | None,
    consumption: _LineConsumption,
) -> str | None:
    """Drain a stream while incrementally decoding and emitting complete lines."""
    if stream is None:
        return "" if consumption.config.capture_output else None

    decoder = _incremental_decoder(consumption.config)
    pending_text = ""

    def feed_decoder(chunk: bytes) -> None:
        """Decode one chunk and emit only lines whose boundary is complete."""
        nonlocal pending_text
        pending_text = _emit_completed_lines(
            pending_text + decoder.decode(chunk),
            on_line=consumption.on_line,
        )

    captured = await consumption.drain(
        stream,
        consumption.config,
        on_chunk=feed_decoder,
        read_size=consumption.read_size,
    )
    pending_text = _emit_completed_lines(
        pending_text + decoder.decode(b"", final=True),
        on_line=consumption.on_line,
    )
    if pending_text:
        consumption.on_line(_strip_line_ending(pending_text))
    return captured


def _incremental_decoder(config: _StreamConfig) -> codecs.IncrementalDecoder:
    """Create the configured incremental decoder."""
    decoder_factory = codecs.getincrementaldecoder(config.encoding)
    return decoder_factory(errors=config.errors)
