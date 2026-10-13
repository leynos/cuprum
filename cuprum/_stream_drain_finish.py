"""How one stream drain reports what it captured.

Split from ``cuprum._streams``, which owns the read/echo/buffer loop. This is
the *result* half of that loop: what a drain hands back once its reads stop,
whether they stopped at EOF or at a cancellation, and the single seam that
decides between the child's own bytes and decoded text.

Keeping the two apart is what makes the mode guarantee reviewable. Both exits
route through one payload renderer here, so a cancelled read and a completed
one cannot disagree about the type they return — an asymmetry a caller could
only discover by timing the cancellation — and each exit names its own
:class:`~cuprum.stream_events.StreamOperationOutcome` exactly once.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._stream_echo import _flush_echo_decoder
from cuprum.stream_events import StreamOperationOutcome
from cuprum.stream_observation import _complete_stream_operation

if typ.TYPE_CHECKING:
    from cuprum._streams import _DrainState, _StreamConfig
    from cuprum.stream_observation import _StreamOperationMeasurement


def _finish_drain(
    state: _DrainState,
    measurement: _StreamOperationMeasurement | None,
    *,
    reached_eof: bool,
) -> str | bytes | None:
    """Complete one drain and return whatever it captured.

    A cancelled read is not a failure to capture: the bytes already read are
    still the child's output, so the partial prefix is returned under the same
    rule as a completed drain. Only the two conditions below — no capture
    buffer, or a caller-requested discard — turn it into a cancellation.

    Returns
    -------
    str | bytes | None
        The captured payload, typed by ``config.capture_bytes``; ``None`` when
        this drain was not capturing.

    Raises
    ------
    asyncio.CancelledError
        If the reads stopped short of EOF, this drain kept a capture buffer,
        and the caller did not ask for a cancellation to discard it.
    """
    if not reached_eof:
        _complete_stream_operation(measurement, StreamOperationOutcome.CANCELLED)
        if state.buffer is None or _discard_on_cancel(state.config):
            raise asyncio.CancelledError
        _flush_echo_decoder(state)
        return _captured_payload(state.buffer, state.config)
    _flush_echo_decoder(state)
    captured = None
    if state.buffer is not None:
        captured = _captured_payload(state.buffer, state.config)
    _complete_stream_operation(measurement, StreamOperationOutcome.EOF)
    return captured


def _captured_payload(buffer: bytearray, config: _StreamConfig) -> str | bytes:
    """Render one drain's buffer as the payload its config asked for.

    The single seam where a captured stream becomes text or stays bytes. Both
    exits of :func:`_finish_drain` route through it so a cancelled read and a
    completed one cannot disagree about the type they hand back. The caller has
    already established that this drain was capturing, so *buffer* is a
    parameter rather than a nullable field read.

    Returns
    -------
    str | bytes
        The child's bytes untouched when ``capture_bytes`` is set, otherwise
        the decoded text.
    """
    if config.capture_bytes:
        # ``bytes(buffer)`` copies, so the returned value cannot alias a
        # bytearray a later drain would go on extending.
        return bytes(buffer)
    return buffer.decode(config.encoding, errors=config.errors)


def _discard_on_cancel(config: _StreamConfig) -> bool:
    """Whether cancellation must discard buffered bytes without decoding them."""
    return config.discard_on_cancel is not None and config.discard_on_cancel.is_set()


__all__ = [
    "_captured_payload",
    "_discard_on_cancel",
    "_finish_drain",
]
