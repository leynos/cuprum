"""Property tests for the canonical stream-drain loop in ``cuprum._streams``.

The ``_drain`` coroutine is the single read/echo/buffer loop shared by
``_consume_stream_without_lines`` and ``_consume_stream_with_lines`` (#115).
These tests drive ``_consume_stream`` directly with a stub reader yielding
arbitrary byte payloads split at arbitrary chunk boundaries — including split
multibyte UTF-8 sequences and invalid bytes — and assert that:

- the captured text equals a whole-payload reference decode for both variants;
- the two variants capture identical text (the parity the canonical loop
  guarantees); and
- line emission is insensitive to chunk boundaries.
"""

from __future__ import annotations

import asyncio
import io
import typing as typ
from collections import Counter

from hypothesis import given
from hypothesis import strategies as st

from cuprum._streams import _consume_stream, _StreamConfig

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _ChunkedReader:
    """Stub stream reader yielding queued chunks then EOF."""

    def __init__(self, chunks: cabc.Sequence[bytes]) -> None:
        """Store the queued chunks to replay."""
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        """Return the next queued chunk, or empty bytes at EOF."""
        await asyncio.sleep(0)
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def _split_at(payload: bytes, cut_points: cabc.Sequence[int]) -> list[bytes]:
    """Split *payload* at the (sorted, deduplicated) *cut_points*."""
    bounds = sorted({point for point in cut_points if 0 < point < len(payload)})
    pieces: list[bytes] = []
    start = 0
    for bound in bounds:
        pieces.append(payload[start:bound])
        start = bound
    pieces.append(payload[start:])
    return [piece for piece in pieces if piece]


@st.composite
def _payload_and_cuts(draw: st.DrawFn) -> tuple[bytes, list[bytes]]:
    """Generate a byte payload and an arbitrary chunking of it."""
    payload = draw(st.binary(min_size=0, max_size=512))
    cut_count = draw(st.integers(min_value=0, max_value=8))
    cut_points = (
        draw(
            st.lists(
                st.integers(min_value=1, max_value=max(1, len(payload) - 1)),
                min_size=cut_count,
                max_size=cut_count,
            ),
        )
        if len(payload) > 1
        else []
    )
    return payload, _split_at(payload, cut_points)


def _config(*, capture: bool = True, echo: bool = False) -> _StreamConfig:
    """Build a UTF-8/replace stream config writing echoes to a StringIO."""
    return _StreamConfig(
        capture_output=capture,
        echo_output=echo,
        sink=io.StringIO(),
        encoding="utf-8",
        errors="replace",
    )


def _decode_chunks(chunks: cabc.Sequence[bytes]) -> str:
    """Decode chunks using the same per-chunk policy as a text echo sink."""
    return "".join(chunk.decode("utf-8", errors="replace") for chunk in chunks)


def _consume(
    chunks: cabc.Sequence[bytes],
    config: _StreamConfig,
    *,
    on_line: cabc.Callable[[str], None] | None = None,
) -> str | None:
    """Run ``_consume_stream`` over the chunk sequence and return the capture."""
    reader = typ.cast("asyncio.StreamReader", _ChunkedReader(chunks))
    return asyncio.run(_consume_stream(reader, config, on_line=on_line))


@given(case=_payload_and_cuts())
def test_drain_capture_matches_reference_decode(
    case: tuple[bytes, list[bytes]],
) -> None:
    """Property: capture equals decoding the whole payload at once.

    Parameters
    ----------
    case : tuple[bytes, list[bytes]]
        Generated payload and an arbitrary chunking of it.
    """
    payload, chunks = case
    captured = _consume(chunks, _config())
    expected = payload.decode("utf-8", errors="replace")
    assert captured == expected, (
        "drained capture must match whole-payload decode for "
        f"payload={payload!r}, chunks={chunks!r}"
    )


@given(case=_payload_and_cuts())
def test_with_and_without_lines_capture_identically(
    case: tuple[bytes, list[bytes]],
) -> None:
    """Property: the line-emitting variant captures the same text.

    The canonical ``_drain`` loop owns the capture mechanics, so layering the
    incremental line decoder on top must not change the captured output.

    Parameters
    ----------
    case : tuple[bytes, list[bytes]]
        Generated payload and an arbitrary chunking of it.
    """
    payload, chunks = case
    plain = _consume(list(chunks), _config())
    with_lines = _consume(list(chunks), _config(), on_line=lambda _line: None)
    assert plain == with_lines, (
        "line-emitting and plain variants must capture identical text for "
        f"payload={payload!r}, chunks={chunks!r}"
    )


@given(case=_payload_and_cuts())
def test_line_emission_is_boundary_insensitive(
    case: tuple[bytes, list[bytes]],
) -> None:
    """Property: emitted lines do not depend on chunk boundaries.

    Feeding the payload as one chunk and as the generated arbitrary chunking
    must produce the same emitted line sequence, even when multibyte UTF-8
    sequences or invalid bytes are split across boundaries.

    Parameters
    ----------
    case : tuple[bytes, list[bytes]]
        Generated payload and an arbitrary chunking of it.
    """
    payload, chunks = case

    whole_lines: list[str] = []
    split_lines: list[str] = []
    _consume([payload] if payload else [], _config(), on_line=whole_lines.append)
    _consume(chunks, _config(), on_line=split_lines.append)

    assert split_lines == whole_lines, (
        "line emission must be independent of chunk boundaries for "
        f"payload={payload!r}, chunks={chunks!r}"
    )


@given(case=_payload_and_cuts())
def test_echo_writes_all_bytes_to_sink(case: tuple[bytes, list[bytes]]) -> None:
    """Property: echoing forwards every chunk to the sink in order.

    Parameters
    ----------
    case : tuple[bytes, list[bytes]]
        Generated payload and an arbitrary chunking of it.
    """
    payload, chunks = case
    config = _config(capture=False, echo=True)
    captured = _consume(chunks, config)

    assert captured is None, "capture disabled must yield None"
    sink = config.sink
    assert isinstance(sink, io.StringIO)
    # The sink has no .buffer, so _write_chunk decodes each chunk separately;
    # per-chunk decoding may replace split sequences differently, so compare
    # against the same per-chunk reference rather than a whole-payload decode.
    expected = _decode_chunks(chunks)
    assert sink.getvalue() == expected, (
        "echo sink must receive every chunk in order for "
        f"payload={payload!r}, chunks={chunks!r}"
    )


@given(case_a=_payload_and_cuts(), case_b=_payload_and_cuts())
def test_concurrent_drains_capture_independently(
    case_a: tuple[bytes, list[bytes]],
    case_b: tuple[bytes, list[bytes]],
) -> None:
    """Property: concurrent drains keep their captures independent.

    Parameters
    ----------
    case_a : tuple[bytes, list[bytes]]
        First generated payload and an arbitrary chunking of it.
    case_b : tuple[bytes, list[bytes]]
        Second generated payload and an arbitrary chunking of it.
    """
    payload_a, chunks_a = case_a
    payload_b, chunks_b = case_b

    async def consume_pair() -> tuple[str | None, str | None]:
        """Run both stream consumers concurrently in one event loop."""
        reader_a = typ.cast("asyncio.StreamReader", _ChunkedReader(chunks_a))
        reader_b = typ.cast("asyncio.StreamReader", _ChunkedReader(chunks_b))
        task_a = asyncio.create_task(_consume_stream(reader_a, _config()))
        task_b = asyncio.create_task(_consume_stream(reader_b, _config()))
        return await asyncio.gather(task_a, task_b)

    captured_a, captured_b = asyncio.run(consume_pair())
    expected_a = payload_a.decode("utf-8", errors="replace")
    expected_b = payload_b.decode("utf-8", errors="replace")

    assert captured_a == expected_a, (
        "first concurrent drain must capture only its own decoded payload for "
        f"payload_a={payload_a!r}, chunks_a={chunks_a!r}, "
        f"payload_b={payload_b!r}, chunks_b={chunks_b!r}"
    )
    assert captured_b == expected_b, (
        "second concurrent drain must capture only its own decoded payload for "
        f"payload_a={payload_a!r}, chunks_a={chunks_a!r}, "
        f"payload_b={payload_b!r}, chunks_b={chunks_b!r}"
    )


@given(case_a=_payload_and_cuts(), case_b=_payload_and_cuts())
def test_concurrent_echo_drains_sink_receives_all_bytes(
    case_a: tuple[bytes, list[bytes]],
    case_b: tuple[bytes, list[bytes]],
) -> None:
    """Property: concurrent echo drains write all decoded chunks to one sink.

    Parameters
    ----------
    case_a : tuple[bytes, list[bytes]]
        First generated payload and an arbitrary chunking of it.
    case_b : tuple[bytes, list[bytes]]
        Second generated payload and an arbitrary chunking of it.
    """
    payload_a, chunks_a = case_a
    payload_b, chunks_b = case_b
    sink = io.StringIO()

    async def consume_pair() -> tuple[str | None, str | None]:
        """Run both echoing stream consumers against the shared sink."""
        reader_a = typ.cast("asyncio.StreamReader", _ChunkedReader(chunks_a))
        reader_b = typ.cast("asyncio.StreamReader", _ChunkedReader(chunks_b))
        config_a = _StreamConfig(
            capture_output=False,
            echo_output=True,
            sink=sink,
            encoding="utf-8",
            errors="replace",
        )
        config_b = _StreamConfig(
            capture_output=False,
            echo_output=True,
            sink=sink,
            encoding="utf-8",
            errors="replace",
        )
        task_a = asyncio.create_task(_consume_stream(reader_a, config_a))
        task_b = asyncio.create_task(_consume_stream(reader_b, config_b))
        return await asyncio.gather(task_a, task_b)

    captured_a, captured_b = asyncio.run(consume_pair())
    actual = sink.getvalue()
    expected = _decode_chunks(chunks_a) + _decode_chunks(chunks_b)

    assert captured_a is None, (
        "first concurrent echo drain must not capture when capture is disabled "
        f"for payload_a={payload_a!r}, chunks_a={chunks_a!r}, "
        f"payload_b={payload_b!r}, chunks_b={chunks_b!r}"
    )
    assert captured_b is None, (
        "second concurrent echo drain must not capture when capture is disabled "
        f"for payload_a={payload_a!r}, chunks_a={chunks_a!r}, "
        f"payload_b={payload_b!r}, chunks_b={chunks_b!r}"
    )
    assert len(actual) == len(expected), (
        "shared echo sink must receive every decoded chunk from both drains "
        f"for payload_a={payload_a!r}, chunks_a={chunks_a!r}, "
        f"payload_b={payload_b!r}, chunks_b={chunks_b!r}, "
        f"actual={actual!r}, expected={expected!r}"
    )
    assert Counter(actual) == Counter(expected), (
        "shared echo sink must receive a permutation of decoded chunks from "
        "both drains "
        f"for payload_a={payload_a!r}, chunks_a={chunks_a!r}, "
        f"payload_b={payload_b!r}, chunks_b={chunks_b!r}, "
        f"actual={actual!r}, expected={expected!r}"
    )
