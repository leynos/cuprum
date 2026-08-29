"""Regression tests for read-size-sensitive stream behaviour."""

from __future__ import annotations

import asyncio
import io

from cuprum._streams import _consume_stream, _StreamConfig
from cuprum._streams_pump import _READ_SIZE


async def _consume_lines(payload: bytes) -> tuple[str | None, list[str]]:
    """Consume *payload* and return its capture and emitted lines."""
    reader = asyncio.StreamReader()
    reader.feed_data(payload)
    reader.feed_eof()
    lines: list[str] = []
    captured = await _consume_stream(reader, _config(), on_line=lines.append)
    return captured, lines
    return reader


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


def test_default_read_size_is_profiled_plateau() -> None:
    """The production default remains the selected 64 KiB plateau."""
    assert _READ_SIZE == 65536, "The profiled 64 KiB default must not regress."
