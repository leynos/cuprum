"""Regression tests for bounded echoing through the stream drain."""

from __future__ import annotations

import asyncio
import io
import typing as typ

from cuprum._streams import _drain, _StreamConfig
from cuprum.echo_events import EchoErrorCategory
from cuprum.echo_observation import observe_echo

_BOUND = 50


class _ChunkedReader:
    """Reader stub yielding queued chunks before EOF."""

    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        """Store chunks for sequential reads without needing an event loop."""
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        """Return the next chunk, or EOF when all chunks were consumed."""
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def _reader(chunks: tuple[bytes, ...]) -> asyncio.StreamReader:
    """Build a reader containing *chunks* and then EOF."""
    return typ.cast("asyncio.StreamReader", _ChunkedReader(chunks))


def _config(
    sink: typ.IO[str],
    *,
    capture: bool,
    errors: str = "replace",
) -> _StreamConfig:
    """Build a bounded UTF-8 echo configuration for one drain regression."""
    return _StreamConfig(
        capture_output=capture,
        echo_output=True,
        echo_max_line_bytes=_BOUND,
        sink=sink,
        encoding="utf-8",
        errors=errors,
    )


def test_echo_only_unterminated_line_keeps_a_bounded_mirror() -> None:
    """capture=False does not retain an oversized unfinished echoed line."""
    payload = b"z" * (2 * 1024 * 1024)
    chunks = tuple(
        payload[start : start + 8192] for start in range(0, len(payload), 8192)
    )
    sink = io.StringIO()

    captured = asyncio.run(
        _drain(_reader(chunks), _config(sink, capture=False)),
    )

    assert captured is None, "echo-only drains must not create captured output"
    assert len(sink.getvalue().encode()) <= _BOUND, (
        "the unfinished echoed line must remain bounded without capture"
    )


class _Cp1252TextOnlySink:
    """Text-only sink recording writes and rejecting unencodable text."""

    def __init__(self) -> None:
        """Record each attempted write payload in order."""
        self.attempts: list[str] = []

    def write(self, payload: str) -> int:
        """Record the write, then reject CP1252-unrepresentable text."""
        self.attempts.append(payload)
        payload.encode("cp1252")
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


def test_rejected_bounded_echo_does_not_observe_truncation() -> None:
    """A sink-rejected bounded write emits only its encoding-failure event."""
    sink = _Cp1252TextOnlySink()
    events = []
    payload = "ś".encode() + b"x" * 80 + b"\n"

    with observe_echo(events.append):
        captured = asyncio.run(
            _drain(
                _reader((payload,)),
                _config(
                    typ.cast("typ.IO[str]", sink),
                    capture=True,
                    errors="strict",
                ),
            )
        )

    assert captured == payload.decode(), "capture must retain the rejected line"
    assert [event.error_category for event in events] == [
        EchoErrorCategory.UNICODE_ENCODE
    ], f"only the failed write must be observed, got={events!r}"
    assert len(sink.attempts) == 1, (
        "disabled echo must not retry later writes or the final decoder flush"
    )
