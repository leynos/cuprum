"""Echo and decoder-flush contracts for the canonical stream drain."""

from __future__ import annotations

import asyncio
import io
import typing as typ

from cuprum._streams import _drain, _StreamConfig
from cuprum.unittests._stream_drain_support import _config, _decode_chunks, _reader


def test_drain_echoes_split_multibyte_text_sink() -> None:
    """Text-sink echoing preserves split characters and flushes decoder tails."""
    chunks = (b"prefix \xe2", b"\x98\x83 suffix \xf0\x9f")
    expected = _decode_chunks(chunks)
    sink = io.StringIO()

    captured = asyncio.run(_drain(_reader(chunks), _config(sink, echo=True)))

    assert captured == expected, (
        f"capture must decode the whole payload for chunks={chunks!r}"
    )
    assert sink.getvalue() == expected, (
        "text-sink echo must preserve split multibyte characters and flush "
        f"incomplete decoder tails for chunks={chunks!r}, expected={expected!r}"
    )


def test_drain_strictly_echoes_split_multibyte_text_sink() -> None:
    """Echo-only drains preserve valid UTF-8 split across text-sink reads."""
    chunks = (b"before \xe2", b"\x98", b"\x83 after")
    sink = io.StringIO()
    config = _StreamConfig(
        capture_output=False,
        echo_output=True,
        sink=sink,
        encoding="utf-8",
        errors="strict",
    )

    captured = asyncio.run(_drain(_reader(chunks), config))

    assert captured is None, f"echo-only drains must return None for chunks={chunks!r}"
    assert sink.getvalue() == "before \u2603 after", (
        "strict text-sink echo must reconstruct split multibyte characters for "
        f"chunks={chunks!r}, received={sink.getvalue()!r}"
    )


def test_drain_echoes_original_bytes_to_buffered_sink() -> None:
    """Buffered echo sinks receive original chunks without text decoding."""
    chunks = (b"before \xe2", b"\x98", b"\x83 after")
    raw_sink = io.BytesIO()
    sink = io.TextIOWrapper(raw_sink, encoding="utf-8")

    captured = asyncio.run(
        _drain(_reader(chunks), _config(sink, capture=False, echo=True)),
    )

    assert captured is None, (
        f"buffered echo-only drains must return None for chunks={chunks!r}"
    )
    assert raw_sink.getvalue() == b"".join(chunks), (
        "buffered echo must preserve original bytes for "
        f"chunks={chunks!r}, received={raw_sink.getvalue()!r}"
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


def test_final_flush_after_disabled_echo_writes_nothing() -> None:
    """A disabled echo never re-attempts the final decoder flush write."""
    sink = _Cp1252TextOnlySink()
    captured = asyncio.run(
        _drain(
            _reader(("ś".encode(), b"\xc3")),
            _config(typ.cast("typ.IO[str]", sink), echo=True),
        ),
    )
    attempts = sink.attempts

    assert captured == "ś\N{REPLACEMENT CHARACTER}", (
        "capture must keep the rejected character and flush the decoder tail "
        f"after echo is disabled for captured={captured!r}"
    )
    assert attempts == ["ś"], (
        "exactly one write must be attempted before echo is disabled, and the "
        f"final decoder flush must stay silent for attempts={attempts!r}"
    )
