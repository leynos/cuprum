"""The public reader over one live output stream.

A ``ByteStreamReader`` is what ``RunningBytesCommand.stdout`` and
``RunningBytesCommand.stderr`` return. It turns the queue's bytes into
``ByteChunk`` values, publishes the queue's terminal state, and translates the
one terminal state that is an error — ``FAILED`` — into a chained
``LiveStreamError``.

The reader owns no task and no pipe. Its lifecycle is the queue's: closing the
reader releases that stream's queue, and settlement releases it if the consumer
never does. That split is what lets a caller close one stream while the other
and the child remain live.
"""

from cuprum._byte_stream.byte_queue import _ByteQueue
from cuprum._process_handle_errors import LiveStreamError
from cuprum.byte_streams import ByteChunk, StreamClosure
from cuprum.echo_events import EchoStream

__all__ = ["ByteStreamReader"]


class ByteStreamReader:
    """An independent byte reader over one stream of a running command.

    Iterate the reader with ``async for`` to receive :class:`ByteChunk` values
    in per-stream order. Iteration ends normally for ``EOF``,
    ``READER_CLOSED``, and ``TRUNCATED_BY_SETTLEMENT``, draining any queued
    chunks first. A ``FAILED`` stream raises
    :class:`~cuprum.sh.LiveStreamError` instead, chained to the original cause.

    ``wait_eof()`` observes only this stream's end, which may arrive while the
    other stream and the child remain live. ``aclose()`` closes only this
    reader; it does not stop the child, the other stream, or the capture.

    The reader is not thread-safe and must be used from the event loop that
    runs the command. One consumer per reader is supported; a second concurrent
    consumer would split the stream between them.

    Examples
    --------
    >>> async for chunk in handle.stdout:
    ...     buffer.extend(chunk.data)
    >>> assert handle.stdout.at_eof
    """

    __slots__ = ("_closed", "_queue", "_sequence", "_stream")

    def __init__(self, stream: EchoStream, queue: _ByteQueue) -> None:
        """Bind one stream name to the queue that feeds it."""
        self._stream = stream
        self._queue = queue
        self._sequence = 0
        self._closed = False

    @property
    def stream(self) -> EchoStream:
        """Which of the child's streams this reader observes.

        Returns
        -------
        EchoStream
            ``EchoStream.STDOUT`` or ``EchoStream.STDERR``.
        """
        return self._stream

    @property
    def closure(self) -> StreamClosure | None:
        """The terminal state, or ``None`` while the reader is live.

        Returns
        -------
        StreamClosure | None
            The first terminal state published for this stream.
        """
        return self._queue.ending

    @property
    def at_eof(self) -> bool:
        """Whether this stream reached a clean end.

        Returns
        -------
        bool
            ``True`` exactly when the terminal state is
            :attr:`~cuprum.byte_streams.StreamClosure.EOF`.
        """
        return self._queue.ending is StreamClosure.EOF

    @property
    def buffered_bytes(self) -> int:
        """The payload bytes queued for this reader and not yet consumed.

        Returns
        -------
        int
            The queued byte count, never above the run's
            ``max_buffered_bytes``.
        """
        return self._queue.buffered_bytes

    def __aiter__(self) -> "ByteStreamReader":
        """Return self; a reader is its own async iterator."""
        return self

    async def __anext__(self) -> ByteChunk:
        """Return the next chunk, or stop once this stream has ended.

        Returns
        -------
        ByteChunk
            The next chunk, with this stream's name and a per-stream sequence
            number.

        Raises
        ------
        StopAsyncIteration
            When the reader was closed, or when its terminal state was reached
            and every queued chunk had been consumed.
        LiveStreamError
            When the terminal state is ``FAILED``. The original failure is
            chained as ``__cause__``.
        """
        if self._closed:
            raise StopAsyncIteration
        data = await self._queue.get()
        if data is None:
            self._closed = True
            failure = self._queue.failure
            if failure is not None:
                raise LiveStreamError(self._stream, cause=failure) from failure
            raise StopAsyncIteration
        chunk = ByteChunk(self._stream, data, self._sequence)
        self._sequence += 1
        return chunk

    async def wait_eof(self) -> StreamClosure:
        """Wait for this stream's terminal state and report it.

        Returns
        -------
        StreamClosure
            ``EOF``, ``READER_CLOSED``, or ``TRUNCATED_BY_SETTLEMENT``.

        Raises
        ------
        LiveStreamError
            When the terminal state is ``FAILED``. The original failure is
            chained as ``__cause__``.
        """
        ending = await self._queue.wait_terminal()
        if ending is StreamClosure.FAILED:
            failure = self._queue.failure
            if failure is not None:
                raise LiveStreamError(self._stream, cause=failure) from failure
            raise LiveStreamError(self._stream)
        return ending

    async def aclose(self) -> None:
        """Close this reader, releasing its queue and later live chunks.

        Idempotent, and safe after the stream has already ended: an existing
        terminal state is never overwritten. The other stream and the child are
        unaffected.
        """
        self._closed = True
        self._queue.close_reader()
