"""Reader-level contracts for live byte streams.

The queue tests prove that bytes are neither lost nor over-retained. These
tests prove what a *caller* sees: per-stream sequence numbers, an EOF that is
observable independently of iteration, a close that stops only one stream, and
the chained ``LiveStreamError`` a failed stream raises from both iteration and
``wait_eof``.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum._byte_stream.byte_queue import _ByteQueue
from cuprum._byte_stream.reader import ByteStreamReader
from cuprum._process_handle_errors import LiveStreamError
from cuprum.byte_streams import ByteChunk, StreamClosure
from cuprum.echo_events import EchoStream


def _reader(capacity: int = 64) -> tuple[ByteStreamReader, _ByteQueue]:
    """Return a stdout reader and the queue behind it."""
    queue = _ByteQueue(capacity)
    return ByteStreamReader(EchoStream.STDOUT, queue), queue


def test_iteration_reports_stream_name_and_sequence() -> None:
    """Every chunk names its stream and carries a zero-based sequence."""

    async def scenario() -> None:
        """Publish two chunks and iterate them."""
        reader, queue = _reader()
        await queue.put(b"one")
        await queue.put(b"two")
        queue.producer_eof()
        chunks = [chunk async for chunk in reader]
        assert [chunk.data for chunk in chunks] == [b"one", b"two"], (
            f"iteration changed the payloads: {chunks!r}"
        )
        assert [chunk.sequence for chunk in chunks] == [0, 1], (
            f"sequence numbers must be per-stream and ordered, got {chunks!r}"
        )
        assert all(chunk.stream is EchoStream.STDOUT for chunk in chunks), (
            "every chunk must name the reader's stream"
        )

    asyncio.run(scenario())


def test_eof_is_observable_before_queued_items_are_consumed() -> None:
    """``wait_eof`` reports the producer's end while items remain queued."""

    async def scenario() -> None:
        """Publish a chunk and EOF, then observe EOF before iterating."""
        reader, queue = _reader()
        await queue.put(b"queued")
        queue.producer_eof()
        ending = await reader.wait_eof()
        assert ending is StreamClosure.EOF, f"expected EOF, got {ending!r}"
        assert reader.at_eof, "at_eof must follow the published EOF"
        assert reader.buffered_bytes == len(b"queued"), (
            "observing EOF must not consume queued bytes"
        )
        chunks = [chunk async for chunk in reader]
        assert [chunk.data for chunk in chunks] == [b"queued"], (
            "the queued item must still be delivered after EOF"
        )

    asyncio.run(scenario())


def test_aclose_stops_iteration_and_reports_reader_closed() -> None:
    """Closing one reader is idempotent and leaves the other stream live."""

    async def scenario() -> None:
        """Close stdout while stderr still holds bytes."""
        stdout, stdout_queue = _reader()
        stderr = ByteStreamReader(EchoStream.STDERR, _ByteQueue(64))
        await stdout_queue.put(b"dropped")
        await stdout.aclose()
        await stdout.aclose()
        assert stdout.closure is StreamClosure.READER_CLOSED, (
            f"a closed reader must report READER_CLOSED, got {stdout.closure!r}"
        )
        assert stdout_queue.buffered_bytes == 0, "a closed reader must release bytes"
        assert [chunk async for chunk in stdout] == [], (
            "a closed reader must yield nothing"
        )
        assert stderr.closure is None, "closing one reader must not touch the other"

    asyncio.run(scenario())


def test_truncation_at_settlement_drains_queued_items_first() -> None:
    """A truncated stream still delivers what it had already queued."""

    async def scenario() -> None:
        """Begin settlement with one queued item, then finalize."""
        reader, queue = _reader()
        await queue.put(b"kept")
        queue.begin_settlement()
        queue.finalize_settlement()
        chunks = [chunk async for chunk in reader]
        assert [chunk.data for chunk in chunks] == [b"kept"], (
            "normal terminal states must drain queued items before stopping"
        )
        assert reader.closure is StreamClosure.TRUNCATED_BY_SETTLEMENT, (
            f"expected truncation, got {reader.closure!r}"
        )

    asyncio.run(scenario())


def test_failed_stream_raises_a_chained_error_from_iteration() -> None:
    """A drain failure reaches the consumer with its cause attached."""

    async def scenario() -> None:
        """Fail a stream that still holds an item."""
        reader, queue = _reader()
        await queue.put(b"pending")
        cause = RuntimeError("drain broke")
        queue.fail(cause)
        with pytest.raises(LiveStreamError) as raised:
            await anext(reader)
        assert raised.value.stream is EchoStream.STDOUT, (
            "the error must name the failed stream"
        )
        assert raised.value.closure is StreamClosure.FAILED, (
            "the error must report FAILED"
        )
        assert raised.value.__cause__ is cause, (
            "the original failure must be chained as the cause"
        )

    asyncio.run(scenario())


def test_failed_stream_raises_from_wait_eof() -> None:
    """``wait_eof`` raises the same chained error as iteration."""

    async def scenario() -> None:
        """Wait on a stream that fails before any item is queued."""
        reader, queue = _reader()
        cause = OSError("pipe broke")
        queue.fail(cause)
        with pytest.raises(LiveStreamError) as raised:
            await reader.wait_eof()
        assert raised.value.__cause__ is cause, (
            "the waiting caller must see the original cause"
        )

    asyncio.run(scenario())


def test_byte_chunk_rejects_values_the_contract_cannot_represent() -> None:
    """A chunk's payload is bytes and its sequence is non-negative."""
    with pytest.raises(TypeError, match="bytes"):
        ByteChunk(EchoStream.STDOUT, typ.cast("bytes", "text"), 0)
    with pytest.raises(ValueError, match="non-negative"):
        ByteChunk(EchoStream.STDOUT, b"data", -1)


def test_close_after_eof_keeps_the_clean_terminal_state() -> None:
    """Closing a reader that already reached EOF does not rewrite history."""

    async def scenario() -> None:
        """Reach EOF, then close."""
        reader, queue = _reader()
        queue.producer_eof()
        await reader.wait_eof()
        await reader.aclose()
        assert reader.closure is StreamClosure.EOF, (
            f"EOF must survive a later close, got {reader.closure!r}"
        )

    asyncio.run(scenario())


def test_failed_iteration_keeps_raising_on_a_later_attempt() -> None:
    """A failed reader never reports ordinary exhaustion on a second pass."""

    async def scenario() -> None:
        """Fail a reader, consume the error, then attempt iteration again."""
        reader, queue = _reader()
        cause = RuntimeError("drain broke")
        queue.fail(cause)
        with pytest.raises(LiveStreamError):
            await anext(reader)
        with pytest.raises(LiveStreamError) as raised:
            await anext(reader)
        assert raised.value.__cause__ is cause, (
            "the repeated attempt must still carry the original cause"
        )

    asyncio.run(scenario())
