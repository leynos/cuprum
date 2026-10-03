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

from cuprum._constants import OBSERVER_ERROR_POLICY
from cuprum._result_assembly import _require_bytes, _require_text
from cuprum._stream_line_boundaries import _split_complete_lines, _strip_line_ending

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc

    from cuprum._streams import _LineSink, _RelayDiagnostics, _StreamConfig
    from cuprum._subprocess_wait_types import _StreamPayload


@dc.dataclass(frozen=True, slots=True)
class _LineConsumption:
    """Inputs for draining a stream while publishing decoded lines."""

    config: _StreamConfig
    on_line: _LineSink
    drain: cabc.Callable[..., cabc.Awaitable[_StreamPayload | None]]
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
) -> str | bytes | None:
    """Drain a stream while incrementally decoding and emitting complete lines.

    The decoder feeds the line sink only; the capture buffer the drain returns
    is untouched, so a byte-exact config still yields the child's own bytes
    here and the widened payload is narrowed by the caller, not by this
    function. A stream that was never attached reports the empty capture in
    the run's own mode, matching
    :func:`cuprum._streams._consume_stream_without_lines`.

    Returns
    -------
    str | bytes | None
        The capture, typed by ``config.capture_bytes``; ``None`` when the run
        captured nothing.
    """
    if stream is None:
        return _empty_capture(consumption.config)

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
    if consumption.config.capture_bytes:
        return _require_bytes(captured, "line-observed stream")
    return _require_text(captured, "line-observed stream")


def _empty_capture(config: _StreamConfig) -> str | bytes | None:
    """Report an unattached stream in the mode the run asked for."""
    if not config.capture_output:
        return None
    return b"" if config.capture_bytes else ""


def _incremental_decoder(config: _StreamConfig) -> codecs.IncrementalDecoder:
    """Create a line-splitting decoder that never fails on invalid bytes.

    Line observation renders a *view* of the child's bytes; it is not where a
    run reports its output, so it must not be able to end the run. The decode
    therefore always replaces undecodable bytes, whatever policy the caller
    chose for the capture. Under ``errors="strict"`` a caller has asked the
    capture to reject invalid bytes, and the capture still does: a byte-exact
    run hands back ``bytes`` untouched, and a text run raises when it decodes
    its buffer in :func:`cuprum._stream_drain_finish._captured_payload`.

    Reading the caller's policy here instead would let an ambient observer
    change the outcome of a run it merely watches. A registered
    ``sh.observe()`` hook, or the line feeder the idle partition attaches,
    supplies a line sink the caller never asked for, and under
    ``errors="strict"`` the resulting :class:`UnicodeDecodeError` would escape
    from the drain's read loop and kill a ``run_bytes()`` that would otherwise
    have returned the child's bytes intact.

    See :data:`cuprum._constants.OBSERVER_ERROR_POLICY` for the shared
    rationale.

    Returns
    -------
    codecs.IncrementalDecoder
        A decoder that replaces undecodable bytes rather than raising.
    """
    decoder_factory = codecs.getincrementaldecoder(config.encoding)
    return decoder_factory(errors=OBSERVER_ERROR_POLICY)
