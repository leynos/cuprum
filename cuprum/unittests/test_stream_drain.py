"""Unit tests for the core capture and cancellation drain contract."""

from __future__ import annotations

import asyncio
import io
import typing as typ

import pytest

from cuprum._streams import _drain, _StreamConfig
from cuprum.unittests._stream_drain_support import (
    CapturedOrNone,
    CapturedPair,
    _ChunkedReader,
    _config,
    _decode_chunks,
    _reader,
)


def test_drain_empty_capture_returns_empty_text() -> None:
    """Empty captured streams drain to an empty string."""
    sink = io.StringIO()
    captured = asyncio.run(_drain(_reader(()), _config(sink)))

    assert not captured, "empty captured streams must decode to empty text"
    assert not sink.getvalue(), "non-echoing empty streams must not write to sink"


def test_drain_forwards_explicit_read_size_to_every_reader_call() -> None:
    """The drain loop retains the injected benchmark read size."""
    reader = _ChunkedReader((b"first", b"second"))
    config = _StreamConfig(
        capture_output=True,
        echo_output=False,
        sink=io.StringIO(),
        encoding="utf-8",
        errors="replace",
        read_size=17,
    )

    captured = asyncio.run(_drain(typ.cast("asyncio.StreamReader", reader), config))

    assert captured == "firstsecond", f"expected complete capture, got {captured!r}"
    assert reader.read_sizes == [17, 17, 17], (
        "every drain reader call must retain the explicit read size, got "
        f"{reader.read_sizes}"
    )


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


def test_discarding_a_cancelled_capture_skips_decoding() -> None:
    """Cleanup cancellation discards an incomplete capture without decoding it."""

    async def run_case() -> None:
        """Cancel a reader after it has buffered invalid but incomplete UTF-8."""
        discard_on_cancel = asyncio.Event()
        reader = asyncio.StreamReader()
        reader.feed_data(b"\xff")
        config = _StreamConfig(
            capture_output=True,
            echo_output=False,
            sink=io.StringIO(),
            encoding="utf-8",
            errors="strict",
            discard_on_cancel=discard_on_cancel,
        )
        task = asyncio.create_task(_drain(reader, config))
        await asyncio.sleep(0)
        discard_on_cancel.set()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_case())


def test_cancelled_capture_retains_buffered_text() -> None:
    """Cancellation returns buffered capture when cleanup does not discard it."""

    async def run_case() -> CapturedOrNone:
        """Cancel a reader after it buffers text and blocks awaiting EOF."""
        reader = asyncio.StreamReader()
        reader.feed_data(b"partial output")
        task = asyncio.create_task(_drain(reader, _config(io.StringIO())))
        await asyncio.sleep(0)
        task.cancel()
        return await task

    assert asyncio.run(run_case()) == "partial output"


def test_cancelled_capture_flushes_replacement_echo() -> None:
    """Cancellation flushes an incomplete echoed character before returning it."""

    async def run_case() -> CapturedPair[str]:
        """Cancel after buffering an incomplete UTF-8 sequence without EOF."""
        reader = asyncio.StreamReader()
        reader.feed_data(b"\xc3")
        sink = io.StringIO()
        task = asyncio.create_task(_drain(reader, _config(sink, echo=True)))
        await asyncio.sleep(0)
        task.cancel()
        return await task, sink.getvalue()

    assert asyncio.run(run_case()) == (
        "\N{REPLACEMENT CHARACTER}",
        "\N{REPLACEMENT CHARACTER}",
    )
