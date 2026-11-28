"""A lossless, byte-bounded queue for one live output stream.

The queue sits between the drain that reads a child's pipe and the reader that
consumes it. Its policy is the opposite of
:class:`~cuprum.BoundedDelivery`'s, and deliberately so: presentation must never
stall the run, while a live observer is part of the run's contract, so a
producer waits for capacity instead of dropping bytes.

Three facts make the implementation small. There is exactly one producer (that
stream's drain) and one consumer (that stream's reader), and both, together
with settlement, run on one event loop. Mutations therefore need no lock; an
``asyncio.Event`` per direction is enough, and clearing it immediately before
the check that follows prevents a lost wake-up because nothing can run in
between.

Terminal state is out of band. A reader can observe the child's end-of-stream
while queued items remain, and iteration then drains them before stopping. A
failure is out of band too, and outranks pending items: a protocol consumer
must see the failure rather than a silently shortened stream.

Once settlement begins the queue stops accepting live chunks. A chunk that does
not fit is discarded and counted, and the count is what lets the reader report
``TRUNCATED_BY_SETTLEMENT`` rather than claiming a clean end it never had.
Capture is unaffected: the drain has already kept the child's bytes.
"""

from __future__ import annotations

import asyncio
import collections

from cuprum._byte_bound import _validate_positive_byte_bound
from cuprum.byte_streams import StreamClosure

__all__ = ["_ByteQueue", "_validate_buffered_bytes"]


def _validate_buffered_bytes(value: object) -> int:
    """Require a strictly positive integer queue capacity.

    Parameters
    ----------
    value : object
        The caller-supplied bound to check.

    Returns
    -------
    int
        The same value, narrowed to ``int``.

    Raises
    ------
    TypeError
        If *value* is not an integer, or is a ``bool``.
    ValueError
        If *value* is not positive.

    Examples
    --------
    >>> _validate_buffered_bytes(1024)
    1024
    """  # ruff: ignore[docstring-extraneous-exception] - the shared validator raises both
    return _validate_positive_byte_bound(value, field="max_buffered_bytes")


class _ByteQueue:
    """A single stream's lossless byte queue and its terminal state.

    Parameters
    ----------
    capacity : int
        The most payload bytes the queue may retain at once. A chunk larger
        than this is split into ordered pieces, each of which waits for space.

    Examples
    --------
    The queue is used from one event loop, with one producer and one consumer::

        queue = _ByteQueue(8)
        await queue.put(b"hello")
        assert await queue.get() == b"hello"
    """

    __slots__ = (
        "_buffered_bytes",
        "_capacity",
        "_capacity_waiting",
        "_data_ready",
        "_discarded_bytes",
        "_ending",
        "_eof_observed",
        "_failure",
        "_items",
        "_settling",
        "_space_ready",
        "_terminal_ready",
    )

    def __init__(self, capacity: int) -> None:
        """Create an empty queue with *capacity* payload bytes of headroom."""
        self._capacity = _validate_buffered_bytes(capacity)
        self._capacity_waiting = 0
        self._items: collections.deque[bytes] = collections.deque()
        self._buffered_bytes = 0
        self._discarded_bytes = 0
        self._eof_observed = False
        self._ending: StreamClosure | None = None
        self._failure: BaseException | None = None
        self._settling = False
        self._data_ready = asyncio.Event()
        self._space_ready = asyncio.Event()
        self._terminal_ready = asyncio.Event()

    @property
    def capacity(self) -> int:
        """The queue's payload-byte bound.

        Returns
        -------
        int
            The configured capacity.
        """
        return self._capacity

    @property
    def buffered_bytes(self) -> int:
        """The payload bytes currently waiting for the consumer.

        Returns
        -------
        int
            The retained byte count, never above :attr:`capacity`.
        """
        return self._buffered_bytes

    @property
    def discarded_bytes(self) -> int:
        """The live bytes dropped after settlement began.

        Returns
        -------
        int
            The discarded count. A non-zero value prevents a later
            ``producer_eof()`` from reporting a clean ``EOF``.
        """
        return self._discarded_bytes

    @property
    def capacity_waiting(self) -> bool:
        """Whether a live ``put`` is parked waiting for capacity right now.

        Returns
        -------
        bool
            ``True`` while a ``put`` awaits space; a full queue alone does not
            prove that, because a read can end exactly at the bound.
        """
        return self._capacity_waiting > 0

    @property
    def ending(self) -> StreamClosure | None:
        """The published terminal state, or ``None`` while the queue is live.

        Returns
        -------
        StreamClosure | None
            The first terminal state published, whichever route published it.
        """
        return self._ending

    @property
    def failure(self) -> BaseException | None:
        """The failure stored out of band, if one was recorded.

        Returns
        -------
        BaseException | None
            The producer-side failure, or ``None`` while none was recorded.
        """
        return self._failure

    async def put(self, data: bytes) -> None:
        """Enqueue *data* losslessly, splitting it to respect the bound.

        While the queue is live the producer waits for capacity; no byte is
        dropped. Once settlement has begun the producer never waits: bytes
        that still fit are enqueued, and only the remainder is discarded and
        counted. That keeps a clean end clean — a tail that fits is delivered,
        so the reader reports ``EOF`` — while still bounding teardown.

        Parameters
        ----------
        data : bytes
            One chunk from the drain. An empty chunk changes nothing.
        """
        if not data:
            return
        offset = 0
        while offset < len(data):
            if self._ending is not None:
                self._discard(len(data) - offset)
                return
            available = await self._reserve()
            if available == 0:
                self._discard(len(data) - offset)
                return
            take = min(available, len(data) - offset)
            self._items.append(data[offset : offset + take])
            self._buffered_bytes += take
            offset += take
            self._data_ready.set()

    async def _reserve(self) -> int:
        """Return the capacity a put may use, or ``0`` when it must discard.

        A live queue waits for its consumer; a released queue never waits, so
        the caller can discard the remainder and let teardown finish.

        Returns
        -------
        int
            The bytes the put may enqueue, or ``0`` when it must discard the
            remainder.
        """
        while True:
            if self._ending is not None:
                return 0
            available = self._capacity - self._buffered_bytes
            if available > 0:
                return available
            if self._settling:
                return 0
            self._space_ready.clear()
            # Re-check after clearing: a consumer that freed space before the
            # clear already set the event, and no other task can run between
            # these statements on this loop.
            if self._capacity - self._buffered_bytes > 0:
                continue
            self._capacity_waiting += 1
            try:
                await self._space_ready.wait()
            finally:
                self._capacity_waiting -= 1

    async def get(self) -> bytes | None:
        """Return the next queued chunk, or ``None`` once it has no more.

        A ``FAILED`` terminal state makes this answer ``None`` immediately,
        even when items remain: a failure outranks pending items by contract.
        The caller distinguishes the two by reading :attr:`failure`, which is
        why the queue does not raise a caller-facing error of its own. Any
        other terminal state drains its queued items first, because a reader
        that reached a clean end is still owed the bytes it had not consumed.

        Returns
        -------
        bytes | None
            The next chunk, or ``None`` when the task should stop — because
            the published state is ``FAILED``, or because a terminal state was
            reached and every queued chunk has been consumed.
        """
        while True:
            if self._ending is StreamClosure.FAILED:
                return None
            if self._items:
                item = self._items.popleft()
                self._buffered_bytes -= len(item)
                self._space_ready.set()
                return item
            if self._ending is not None:
                return None
            self._data_ready.clear()
            if self._ending is StreamClosure.FAILED:
                continue
            if self._items or self._ending is not None:
                continue
            await self._data_ready.wait()

    async def wait_terminal(self) -> StreamClosure:
        """Wait until a terminal state is published and return it.

        Returns
        -------
        StreamClosure
            The published terminal state, including ``FAILED``. A caller that
            must report the failure reads :attr:`failure` as well, so the queue
            never raises a caller-facing error of its own.
        """
        while self._ending is None:
            self._terminal_ready.clear()
            if self._ending is not None:
                break
            await self._terminal_ready.wait()
        return self._ending

    def note_eof(self) -> None:
        """Record that the drain observed the child's genuine end-of-stream.

        This publishes no terminal state. The drain has not finalized its
        capture yet, and a finalization failure must be able to publish
        ``FAILED`` before any reader observes a clean end. :meth:`complete`
        publishes the end once the drain task succeeds.
        """
        self._eof_observed = True

    def complete(self) -> None:
        """Publish the end a successful drain task observed, if it saw one.

        A clean ``EOF`` requires both an observed end and no discarded byte.
        Settlement may have begun before the drain observed the end — that is
        the ordinary case, because the exit wait can return before the final
        read — but if every chunk still fitted the queue, the reader did
        receive the whole stream and ``EOF`` is the honest state. Any
        discarded byte makes the terminal state ``TRUNCATED_BY_SETTLEMENT``
        instead. A drain that never observed an end is left to settlement's
        own finalization. An already published state is never overwritten.
        """
        if self._ending is not None or not self._eof_observed:
            return
        if self._discarded_bytes:
            self._publish(StreamClosure.TRUNCATED_BY_SETTLEMENT)
        else:
            self._publish(StreamClosure.EOF)

    def begin_settlement(self) -> None:
        """Stop waiting for space and wake a producer blocked on capacity.

        This is the provisional half of settlement. It publishes no status:
        the drain may still reach a clean EOF during the reconciliation
        window, and :meth:`finalize_settlement` publishes the truncation only
        for a queue that never got one. A chunk that fits is still enqueued;
        only the remainder is discarded.
        """
        self._settling = True
        self._space_ready.set()

    def finalize_settlement(self) -> None:
        """Publish ``TRUNCATED_BY_SETTLEMENT`` if no state exists yet.

        Called once reconciliation ends, so a reader waiting on a queue that
        never reached a clean end is released rather than left waiting.
        """
        if self._ending is None:
            self._publish(StreamClosure.TRUNCATED_BY_SETTLEMENT)

    def fail(self, cause: BaseException) -> None:
        """Publish ``FAILED`` with *cause* stored out of band.

        The first terminal state is final. A failure that arrives before any
        other state becomes ``FAILED`` and releases the queued items, because
        a failed stream cannot deliver them. A failure recorded after a state
        was already published is retained for the run's settlement without
        rewriting the reader's closure or discarding the bytes it is still
        owed. The capture holds the stream either way.

        Parameters
        ----------
        cause : BaseException
            The drain or supervisor failure the reader must report.
        """
        if self._failure is None:
            self._failure = cause
        self._settling = True
        if self._ending is not None:
            return
        self._items.clear()
        self._buffered_bytes = 0
        self._publish(StreamClosure.FAILED)

    def close_reader(self) -> None:
        """Publish ``READER_CLOSED`` and discard queued items.

        Idempotent, and it never overrides a state already published. The
        queue's bytes are released because the consumer has said it will not
        read them, and later live chunks are discarded rather than buffered.
        """
        self._items.clear()
        self._buffered_bytes = 0
        if self._ending is not None:
            return
        self._settling = True
        self._publish(StreamClosure.READER_CLOSED)

    def _publish(self, ending: StreamClosure) -> None:
        """Record *ending* and wake every waiter, exactly once."""
        if self._ending is not None:
            return
        self._ending = ending
        self._release_waiters()

    def _release_waiters(self) -> None:
        """Wake the producer, the consumer, and any terminal waiter."""
        self._space_ready.set()
        self._data_ready.set()
        self._terminal_ready.set()

    def _discard(self, byte_count: int) -> None:
        """Account for live bytes dropped after settlement began."""
        if byte_count > 0:
            self._discarded_bytes += byte_count
