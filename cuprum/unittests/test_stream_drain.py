"""Unit tests for the core capture and cancellation drain contract."""

from __future__ import annotations

import asyncio
import io
import typing as typ

import pytest

from cuprum._stream_line_consumer import _consume_stream_with_lines, _LineConsumption
from cuprum._streams import _drain, _StreamConfig
from cuprum.capture_policy import FullCapture
from cuprum.unittests._stream_drain_support import (
    CapturedPair,
    _ChunkedReader,
    _config,
    _decode_chunks,
    _reader,
)

if typ.TYPE_CHECKING:
    from cuprum._stream_drain_finish import _CapturedDrain


def test_drain_empty_capture_returns_empty_text() -> None:
    """Empty captured streams drain to an empty string."""
    sink = io.StringIO()
    drained = asyncio.run(_drain(_reader(()), _config(sink)))

    assert not drained.payload, "empty captured streams must decode to empty text"
    assert drained.capture is not None, (
        "a captured stream must report a record when its payload is empty"
    )
    assert drained.reached_eof is True, "an empty drain still reached end of stream"
    assert not sink.getvalue(), "non-echoing empty streams must not write to sink"


def test_drain_forwards_explicit_read_size_to_every_reader_call() -> None:
    """The drain loop retains the injected benchmark read size."""
    reader = _ChunkedReader((b"first", b"second"))
    config = _StreamConfig(
        capture=FullCapture(),
        echo_output=False,
        sink=io.StringIO(),
        encoding="utf-8",
        errors="replace",
        read_size=17,
    )

    captured = asyncio.run(_drain(typ.cast("asyncio.StreamReader", reader), config))

    assert captured.payload == "firstsecond", (
        f"expected complete capture, got {captured!r}"
    )
    assert reader.read_sizes == [17, 17, 17], (
        "every drain reader call must retain the explicit read size, got "
        f"{reader.read_sizes}"
    )


def test_drain_respects_capture_and_echo_flags() -> None:
    """Capture and echo flags independently control drain side effects."""
    chunks = (b"alpha", " \u2603".encode())
    sink = io.StringIO()
    captured = asyncio.run(_drain(_reader(chunks), _config(sink, echo=True)))

    assert captured.payload == b"alpha \xe2\x98\x83".decode(), (
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
        _drain(_reader(chunks), _config(sink, capture=None, echo=True)),
    )

    assert captured.payload is None, "capture-disabled drains must return no payload"
    assert captured.capture is None, "capture-disabled drains must report no record"
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
            capture=FullCapture(),
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

    async def run_case() -> _CapturedDrain:
        """Cancel a reader after it buffers text and blocks awaiting EOF."""
        reader = asyncio.StreamReader()
        reader.feed_data(b"partial output")
        task = asyncio.create_task(_drain(reader, _config(io.StringIO())))
        await asyncio.sleep(0)
        task.cancel()
        return await task

    drained = asyncio.run(run_case())
    assert drained.payload == "partial output", (
        f"the buffered prefix must survive cancellation, got {drained.payload!r}"
    )
    assert drained.reached_eof is False, (
        "a cancelled read stopped short of end of stream, so the drain must say so"
    )


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
        return (await task).payload, sink.getvalue()

    assert asyncio.run(run_case()) == (
        "\N{REPLACEMENT CHARACTER}",
        "\N{REPLACEMENT CHARACTER}",
    )


def test_line_consumer_preserving_cancellation_reports_a_short_drain() -> None:
    """A preserving line-stream teardown keeps its prefix and marks the stop."""

    async def run_case() -> _CapturedDrain:
        """Cancel a line-observing drain while its settlement is preserving."""
        settlement_started = asyncio.Event()
        settlement_started.set()
        config = _StreamConfig(
            capture=FullCapture(),
            echo_output=False,
            sink=io.StringIO(),
            encoding="utf-8",
            errors="replace",
            settlement_started=settlement_started,
        )

        # RUF029: the drain seam is awaited by the consumer, so the double must
        # be a coroutine function even though its first step is the raise.
        async def cancel_drain(  # ruff: ignore[unused-async]
            *_args: object,
            **_kwargs: object,
        ) -> _CapturedDrain:
            """Stop the drain at the point a cancellation reaches it."""
            raise asyncio.CancelledError

        stream = typ.cast("asyncio.StreamReader", object())
        return await _consume_stream_with_lines(
            stream,
            _LineConsumption(
                config=config,
                on_line=lambda _line: None,
                drain=cancel_drain,
                relay_diagnostics=None,
            ),
        )

    drained = asyncio.run(run_case())
    assert drained.capture is not None, (
        "a preserving teardown must still report the capture it kept"
    )
    assert drained.reached_eof is False, (
        "a cancellation before end of stream must not read as a completed drain"
    )
