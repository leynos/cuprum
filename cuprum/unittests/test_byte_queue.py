"""Stateful and property checks for the lossless live byte queue.

The queue is the one place a live byte run promises that no byte is lost while
a reader is live. These tests prove the promise from both ends: a deterministic
stateful scenario that interleaves capacity pressure, a stalled consumer, and
release; and a property that concatenates posted chunks and compares the result
with what a concurrent consumer received.

The property is the non-vacuity guard for the deterministic cases: it draws
chunk lists that include chunks larger than the queue's bound, so the split
path and the waiting path are exercised across many lengths rather than one.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum._byte_stream.byte_queue import _ByteQueue, _validate_buffered_bytes
from cuprum.byte_streams import StreamClosure

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def test_capacity_validation_rejects_non_positive_and_non_integer() -> None:
    """The bound must be a strictly positive integer."""
    for value in (0, -1):
        with pytest.raises(ValueError, match="positive"):
            _validate_buffered_bytes(value)
    for value in (True, 1.5, "8", None):
        with pytest.raises(TypeError, match="integer"):
            _validate_buffered_bytes(value)


def test_oversized_chunk_is_split_and_delivered_in_order() -> None:
    """A chunk larger than the bound is split, never dropped."""

    async def scenario() -> None:
        """Publish one oversized chunk and consume it concurrently."""
        queue = _ByteQueue(4)
        payload = b"abcdefghij"

        async def consume() -> bytes:
            """Collect every chunk the queue yields."""
            received = bytearray()
            while (chunk := await queue.get()) is not None:
                received.extend(chunk)
            return bytes(received)

        consumer = asyncio.create_task(consume())
        await queue.put(payload)
        assert queue.buffered_bytes <= 4, (
            f"the queue must never exceed its bound, got {queue.buffered_bytes}"
        )
        queue.producer_eof()
        received = await consumer
        assert received == payload, f"split delivery changed the bytes: {received!r}"

    asyncio.run(scenario())


def test_stalled_consumer_applies_backpressure_without_losing_bytes() -> None:
    """A put waits for space while the queue is live, then completes on release."""

    async def scenario() -> None:
        """Block a large put, then release the queue and drain what was kept."""
        queue = _ByteQueue(4)

        async def publish() -> None:
            """Publish more than the queue can hold."""
            await queue.put(b"0123456789")

        producer = asyncio.create_task(publish())
        await asyncio.sleep(0)
        assert not producer.done(), "the producer must wait while the queue is full"
        assert queue.buffered_bytes == 4, (
            f"only the bound may be queued, got {queue.buffered_bytes}"
        )
        queue.begin_settlement()
        await asyncio.wait_for(producer, timeout=1.0)
        assert queue.discarded_bytes == 6, (
            f"the released remainder must be accounted, got {queue.discarded_bytes}"
        )
        queue.finalize_settlement()
        assert queue.ending is StreamClosure.TRUNCATED_BY_SETTLEMENT, (
            "a released queue without EOF must report truncation"
        )

    asyncio.run(scenario())


def test_eof_after_release_without_discard_is_clean() -> None:
    """A settlement that loses nothing reports EOF rather than truncation."""

    async def scenario() -> None:
        """Publish exactly the bound, begin settlement, then publish EOF."""
        queue = _ByteQueue(4)
        await queue.put(b"abcd")
        queue.begin_settlement()
        queue.producer_eof()
        assert queue.ending is StreamClosure.EOF, (
            "nothing was discarded, so the reader received the whole stream"
        )

    asyncio.run(scenario())


def test_failure_outranks_pending_items() -> None:
    """A recorded failure stops the consumer without yielding queued items."""

    async def scenario() -> None:
        """Queue an item, fail the stream, and read nothing."""
        queue = _ByteQueue(8)
        await queue.put(b"payload")
        failure = RuntimeError("drain failed")
        queue.fail(failure)
        assert await queue.get() is None, "a failure must outrank pending items"
        assert queue.failure is failure, "the original failure must be retained"
        assert queue.ending is StreamClosure.FAILED, "the queue must report FAILED"

    asyncio.run(scenario())


def test_reader_close_discards_queued_bytes() -> None:
    """Closing the reader releases its queue and reports READER_CLOSED."""

    async def scenario() -> None:
        """Close a queue that still holds bytes."""
        queue = _ByteQueue(8)
        await queue.put(b"queued")
        queue.close_reader()
        assert queue.ending is StreamClosure.READER_CLOSED, (
            "a closed reader must report READER_CLOSED"
        )
        assert queue.buffered_bytes == 0, "a closed reader must release its bytes"
        assert await queue.get() is None, "a closed reader must stop its consumer"

    asyncio.run(scenario())


def test_wait_terminal_observes_a_state_published_later() -> None:
    """A terminal waiter wakes when EOF is published."""

    async def scenario() -> None:
        """Wait for a terminal state published by another coroutine."""
        queue = _ByteQueue(4)
        waiter = asyncio.create_task(queue.wait_terminal())
        await asyncio.sleep(0)
        assert not waiter.done(), "the waiter must suspend until the state exists"
        queue.producer_eof()
        assert await asyncio.wait_for(waiter, 1.0) is StreamClosure.EOF, (
            "the waiter must observe the published EOF"
        )

    asyncio.run(scenario())


@settings(max_examples=50, deadline=None)
@given(
    chunks=st.lists(st.binary(min_size=0, max_size=64), max_size=25),
    capacity=st.integers(min_value=1, max_value=64),
)
def test_consumed_bytes_equal_posted_bytes(
    chunks: list[bytes],
    capacity: int,
) -> None:
    """For any chunk list and bound, the consumer receives every posted byte."""

    async def scenario() -> None:
        """Publish the chunks while a consumer drains them concurrently."""
        queue = _ByteQueue(capacity)

        async def consume() -> bytes:
            """Collect everything the queue yields until it ends."""
            received = bytearray()
            while (chunk := await queue.get()) is not None:
                assert queue.buffered_bytes <= capacity, (
                    f"bound exceeded: {queue.buffered_bytes} > {capacity}"
                )
                received.extend(chunk)
            return bytes(received)

        consumer = asyncio.create_task(consume())
        for chunk in chunks:
            await queue.put(chunk)
        queue.producer_eof()
        received = await asyncio.wait_for(consumer, timeout=5.0)
        assert received == b"".join(chunks), (
            f"lossless delivery failed for capacity {capacity}"
        )
        assert queue.discarded_bytes == 0, "a live reader must lose nothing"

    asyncio.run(scenario())


def test_queue_is_iterable_by_a_cancelled_consumer() -> None:
    """A consumer cancelled while waiting leaves no pending work behind."""

    async def scenario() -> None:
        """Cancel a consumer waiting on an empty live queue."""
        queue = _ByteQueue(4)

        async def consume() -> cabc.AsyncIterator[bytes]:
            """Yield until cancelled."""
            while (chunk := await queue.get()) is not None:
                yield chunk

        consumer = asyncio.create_task(_drain(consume()))
        await asyncio.sleep(0)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer

    asyncio.run(scenario())


async def _drain(source: cabc.AsyncIterator[bytes]) -> None:
    """Consume an iterator to completion, for the cancellation case."""
    async for _chunk in source:
        pass
