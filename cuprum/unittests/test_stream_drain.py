"""Unit properties for the canonical stream-drain helper.

The public pipeline tests cover ``_drain`` through real subprocess I/O.  This
module keeps a small direct contract around the canonical helper itself so the
two ``_consume_stream`` variants cannot diverge silently during refactors.
It uses a stub ``asyncio.StreamReader`` shape to provide deterministic chunk
boundaries, including split UTF-8 sequences and invalid byte payloads.
"""

from __future__ import annotations

import asyncio
import io
import typing as typ

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from cuprum._streams import _consume_stream, _drain, _StreamConfig

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_PROPERTY_MAX_EXAMPLES = 24


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


def _config(
    sink: typ.IO[str],
    *,
    capture: bool = True,
    echo: bool = False,
) -> _StreamConfig:
    """Build a UTF-8 stream config for direct drain tests."""
    return _StreamConfig(
        capture_output=capture,
        echo_output=echo,
        sink=sink,
        encoding="utf-8",
        errors="replace",
    )


def _split_at(payload: bytes, cut_points: cabc.Sequence[int]) -> tuple[bytes, ...]:
    """Split a payload at sorted, deduplicated cut points."""
    bounds = sorted({point for point in cut_points if 0 < point < len(payload)})
    pieces: list[bytes] = []
    start = 0
    for bound in bounds:
        pieces.append(payload[start:bound])
        start = bound
    pieces.append(payload[start:])
    return tuple(piece for piece in pieces if piece)


@st.composite
def _payload_and_chunks(draw: st.DrawFn) -> tuple[bytes, tuple[bytes, ...]]:
    """Generate payload bytes and arbitrary stream chunks."""
    payload = draw(st.binary(min_size=0, max_size=512))
    if len(payload) <= 1:
        return payload, (payload,) if payload else ()

    cut_points = draw(
        st.lists(
            st.integers(min_value=1, max_value=len(payload) - 1),
            min_size=0,
            max_size=min(8, len(payload) - 1),
            unique=True,
        ),
    )
    return payload, _split_at(payload, cut_points)


def _decode_chunks(chunks: cabc.Sequence[bytes]) -> str:
    """Decode chunks as one stream for comparison with text-sink echoing."""
    return b"".join(chunks).decode("utf-8", errors="replace")


def _expected_emitted_lines(payload: bytes) -> list[str]:
    """Model stream line emission while preserving non-CR/LF boundaries."""
    lines = payload.decode("utf-8", errors="replace").splitlines(keepends=True)
    if not lines:
        return []

    remainder = ""
    if not lines[-1].endswith(("\n", "\r")):
        remainder = lines.pop()

    def strip_ending(line: str) -> str:
        """Strip the CR/LF endings recognised by stream line callbacks."""
        if line.endswith("\r\n"):
            return line[:-2]
        if line.endswith(("\n", "\r")):
            return line[:-1]
        return line

    emitted = [strip_ending(line) for line in lines]
    if remainder:
        emitted.append(strip_ending(remainder))
    return emitted


def test_drain_empty_capture_returns_empty_text() -> None:
    """Empty captured streams drain to an empty string."""
    sink = io.StringIO()
    captured = asyncio.run(_drain(_reader(()), _config(sink)))

    assert captured == "", "empty captured streams must decode to empty text"
    assert sink.getvalue() == "", "non-echoing empty streams must not write to sink"


def test_drain_respects_capture_and_echo_flags() -> None:
    """Capture and echo flags independently control drain side effects."""
    chunks = (b"alpha", " \u2603".encode())
    sink = io.StringIO()
    captured = asyncio.run(_drain(_reader(chunks), _config(sink, echo=True)))

    assert captured == b"alpha \xe2\x98\x83".decode(), (
        f"captured text must decode the complete payload for chunks={chunks!r}"
    )
    assert sink.getvalue() == _decode_chunks(chunks), (
        f"echo sink must receive every chunk for chunks={chunks!r}"
    )


def test_drain_can_disable_capture_while_echoing() -> None:
    """Echo-only drains write chunks but return no captured text."""
    chunks = (b"only ", b"echo")
    sink = io.StringIO()
    captured = asyncio.run(
        _drain(_reader(chunks), _config(sink, capture=False, echo=True)),
    )

    assert captured is None, "capture-disabled drains must return None"
    assert sink.getvalue() == "only echo", (
        f"echo-only drain must write all decoded chunks for chunks={chunks!r}"
    )


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


@settings(
    max_examples=_PROPERTY_MAX_EXAMPLES,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(case=_payload_and_chunks())
def test_consume_stream_variants_capture_identically(
    case: tuple[bytes, tuple[bytes, ...]],
) -> None:
    """Property: line and plain consume variants capture identical text."""
    payload, chunks = case
    plain_sink = io.StringIO()
    line_sink = io.StringIO()

    plain = asyncio.run(_consume_stream(_reader(chunks), _config(plain_sink)))
    with_lines = asyncio.run(
        _consume_stream(
            _reader(chunks),
            _config(line_sink),
            on_line=lambda _line: None,
        ),
    )

    assert plain == with_lines == payload.decode("utf-8", errors="replace"), (
        "plain and line-emitting variants must match whole-payload decode for "
        f"payload={payload!r}, chunks={chunks!r}"
    )


@settings(
    max_examples=_PROPERTY_MAX_EXAMPLES,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@example(case=(b"first\nsecond\n", (b"first\nsec", b"ond\n")))
@given(case=_payload_and_chunks())
def test_line_emission_is_chunk_boundary_insensitive(
    case: tuple[bytes, tuple[bytes, ...]],
) -> None:
    """Property: line emission is independent of stream chunk boundaries."""
    payload, chunks = case
    expected_lines = _expected_emitted_lines(payload)
    whole_lines: list[str] = []
    split_lines: list[str] = []

    asyncio.run(
        _consume_stream(
            _reader((payload,) if payload else ()),
            _config(io.StringIO()),
            on_line=whole_lines.append,
        ),
    )
    asyncio.run(
        _consume_stream(
            _reader(chunks),
            _config(io.StringIO()),
            on_line=split_lines.append,
        ),
    )

    assert whole_lines == expected_lines, (
        "whole-payload line emission must match decoded payload lines for "
        f"payload={payload!r}, chunks={chunks!r}, expected={expected_lines!r}"
    )
    assert split_lines == expected_lines, (
        "split-payload line emission must match decoded payload lines for "
        f"payload={payload!r}, chunks={chunks!r}, expected={expected_lines!r}"
    )
    assert split_lines == whole_lines, (
        "line emission must not depend on chunk boundaries for "
        f"payload={payload!r}, chunks={chunks!r}"
    )
