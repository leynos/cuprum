"""Bounded line observation inside the canonical stream drain.

Line observation is the one drain mode that keeps decoded text beyond the
chunk being decoded: it must hold an incomplete line until its boundary
arrives. ``max_line_chars`` bounds how much that holding may retain. These
tests drive ``_consume_stream`` directly with a chunked reader, so they can
pin fragment delivery, the at-bound case, the unbounded opt-out, and the scan
cost without a real child process.
"""

from __future__ import annotations

import asyncio
import io
import typing as typ

from cuprum._streams import _consume_stream, _StreamConfig

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import re

    import pytest


class _ChunkedReader:
    """Stub stream reader yielding queued chunks before EOF."""

    def __init__(self, chunks: cabc.Sequence[bytes]) -> None:
        """Store chunks for sequential ``read`` calls."""
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        """Return the next queued chunk, or empty bytes at EOF."""
        await asyncio.sleep(0)
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def _reader(chunks: cabc.Sequence[bytes]) -> asyncio.StreamReader:
    """Build a stream-reader-shaped stub for the given chunks."""
    return typ.cast("asyncio.StreamReader", _ChunkedReader(chunks))


def _config(max_line_chars: int | None) -> _StreamConfig:
    """Build a capturing UTF-8 stream config with the given line bound."""
    return _StreamConfig(
        capture_output=True,
        echo_output=False,
        sink=io.StringIO(),
        encoding="utf-8",
        errors="replace",
        max_line_chars=max_line_chars,
    )


def test_line_observer_bounds_an_unterminated_run_as_it_arrives() -> None:
    """A newline-free run is published in bounded fragments, not retained whole.

    The child never terminates this line, so a drain that waited for a
    boundary would hold the entire payload until EOF. The configured bound is
    small enough that the fragments are visible in the event stream.
    """
    chunks = (b"a" * 10, b"b" * 10)
    lines: list[str] = []

    captured = asyncio.run(
        _consume_stream(
            _reader(chunks),
            _config(max_line_chars=4),
            on_line=lines.append,
        )
    )

    assert captured == "a" * 10 + "b" * 10, (
        f"capture must stay complete, got {captured!r}"
    )
    assert all(len(line) <= 4 for line in lines), (
        f"every observed event must respect the bound, got {lines!r}"
    )
    assert "".join(lines) == "a" * 10 + "b" * 10, (
        f"fragments must preserve the child's text, got {lines!r}"
    )


def test_line_observer_bounds_a_terminated_line_at_the_bound() -> None:
    """A line of exactly the bound followed by a terminator yields one event."""
    payload = b"x" * 8 + b"\n"
    lines: list[str] = []

    asyncio.run(
        _consume_stream(
            _reader((payload,)),
            _config(max_line_chars=8),
            on_line=lines.append,
        )
    )

    assert lines == ["x" * 8], (
        f"a line at the bound must not also emit an empty fragment, got {lines!r}"
    )


def test_line_observer_without_a_bound_keeps_the_whole_line() -> None:
    """``max_line_chars=None`` restores one event for an unterminated line."""
    payload = b"y" * 5000
    lines: list[str] = []

    asyncio.run(
        _consume_stream(
            _reader((payload,)),
            _config(max_line_chars=None),
            on_line=lines.append,
        )
    )

    assert lines == ["y" * 5000], (
        f"``None`` must not fragment the line, got {len(lines)} event(s)"
    )


def test_line_observer_scans_each_decoded_character_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drain's work is linear in the decoded text, not quadratic.

    A quadratic implementation rescans the retained prefix for every chunk, so
    the number of characters visited grows with the square of the line length.
    The spy counts the characters the boundary search is asked to cover; the
    assertion is a ratio between two line lengths, which makes it independent
    of the machine's speed. A wall-clock comparison would measure load instead.

    Both lines arrive as single-character chunks, so the quadratic version
    rescans a longer and longer prefix: doubling the line length quadruples the
    work there but only doubles it for one-pass scanning. The bound is wider
    than either line, so no fragment splits a read and the comparison measures
    the scan rather than the number of emitted events.
    """
    from cuprum import _stream_line_boundaries

    scanned: list[int] = []
    original = _stream_line_boundaries._find_next_boundary

    def counting_search(text: str, start: int) -> re.Match[str] | None:
        """Record the characters a boundary search covers, then delegate."""
        scanned.append(len(text) - start)
        return original(text, start)

    def total_scanned(chunk_count: int) -> int:
        """Drain one unterminated line in single-character chunks."""
        scanned.clear()
        asyncio.run(
            _consume_stream(
                _reader(tuple(b"z" for _ in range(chunk_count))),
                _config(max_line_chars=2 * chunk_count),
                on_line=lambda _line: None,
            )
        )
        return sum(scanned)

    monkeypatch.setattr(
        _stream_line_boundaries,
        "_find_next_boundary",
        counting_search,
    )
    short = total_scanned(8)
    long = total_scanned(16)

    assert short > 0, "the spy must observe the boundary search it measures"
    assert long <= 3 * short, (
        "a doubled line must not more than triple the scanned characters; "
        f"short={short}, long={long} suggests rescanning retained text"
    )
