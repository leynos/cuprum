"""Regression tests for read-size-sensitive stream behaviour."""

from __future__ import annotations

import asyncio
import io

from cuprum._streams import _consume_stream, _StreamConfig
from cuprum._streams_pump import _READ_SIZE


class _RecordingReader(asyncio.StreamReader):
    """Stream reader recording each requested read size."""

    def __init__(self, payload: bytes) -> None:
        """Buffer the payload and mark its end of file."""
        super().__init__()
        self.read_sizes: list[int] = []
        self.feed_data(payload)
        self.feed_eof()

    async def read(self, n: int = -1) -> bytes:
        """Record the requested size before reading buffered bytes."""
        self.read_sizes.append(n)
        return await super().read(n)


async def _consume_lines(payload: bytes) -> tuple[str | None, list[str]]:
    """Consume *payload* and return its capture and emitted lines."""
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    lines: list[str] = []
    captured = await _consume_stream(reader, _config(), on_line=lines.append)
    return captured, lines


def _config() -> _StreamConfig:
    """Return a UTF-8 config that captures output without echoing it."""
    return _StreamConfig(
        capture_output=True,
        echo_output=False,
        sink=io.StringIO(),
        encoding="utf-8",
        errors="strict",
    )


def test_crlf_split_at_read_boundary_emits_no_empty_line() -> None:
    """A CRLF pair split by the read boundary remains one line ending."""
    first_line = "a" + "x" * (_READ_SIZE - 2)

    captured, lines = asyncio.run(_consume_lines(f"{first_line}\r\nb".encode()))

    assert captured == f"{first_line}\r\nb"
    assert lines == [first_line, "b"]


def test_consume_stream_forwards_explicit_read_size_to_every_reader_call() -> None:
    """The final stream consumer retains the injected benchmark read size."""

    async def consume() -> tuple[str | None, list[int]]:
        """Consume a recording reader with a deliberately non-default size."""
        reader = _RecordingReader(b"firstsecond")
        captured = await _consume_stream(reader, _config(), read_size=17)
        return captured, reader.read_sizes

    captured, read_sizes = asyncio.run(consume())

    assert captured == "firstsecond", f"expected complete capture, got {captured!r}"
    assert read_sizes == [17, 17], (
        "every consumer reader call must retain the explicit read size, got "
        f"{read_sizes}"
    )


def test_default_read_size_is_profiled_plateau() -> None:
    """The production default remains within the profiled plateau range."""
    assert 16384 <= _READ_SIZE <= 65536, (
        "docs/tee-hotpath-read-size-sweep-2026-08-29.md requires the "
        "profiled read size to remain in the approved 16-64 KiB range, got "
        f"{_READ_SIZE}"
    )
