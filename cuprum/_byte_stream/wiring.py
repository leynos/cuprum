"""Build the queues and readers one byte run owns.

The bundle built here is the whole live half of a byte run: two queues, two
readers, and the per-stream taps the stream spawn attaches to the ordinary
drains. Keeping the construction in one place is what guarantees that the
queue a reader consumes is the queue the matching tap fills — the stdout reader
cannot be wired to stderr's queue.

The bundle also owns the two settlement transitions every queue must receive,
so the handle and its settlement task cannot disagree about whether a stream
was released or finalized.
"""

from __future__ import annotations

import dataclasses as dc
import functools

from cuprum._byte_stream.byte_queue import _ByteQueue, _validate_buffered_bytes
from cuprum._byte_stream.reader import ByteStreamReader
from cuprum._stream_live import _LiveStreamSink, _LiveStreamSinks, _StopRequest
from cuprum.echo_events import EchoStream

__all__ = ["_ByteStreams", "build_byte_streams"]


@dc.dataclass(frozen=True, slots=True)
class _QueueTap:
    """Adapt one queue to the drain's chunk and end-of-stream callbacks."""

    queue: _ByteQueue

    async def on_chunk(self, chunk: bytes) -> None:
        """Enqueue one chunk, waiting for capacity while the queue is live.

        Parameters
        ----------
        chunk : bytes
            One chunk from the drain's read loop.
        """
        await self.queue.put(chunk)


@dc.dataclass(frozen=True, slots=True)
class _ByteStreams:
    """Both live streams of one byte run, and their settlement transitions.

    The queues are retained rather than reached through the readers because
    settlement must be able to release, fail, and finalize a queue even when
    the caller never touches that reader. The readers are the public faces of
    the same objects, so a queue and its reader cannot be mismatched.
    """

    stdout_queue: _ByteQueue
    stderr_queue: _ByteQueue
    stdout: ByteStreamReader
    stderr: ByteStreamReader
    stop: _StopRequest = dc.field(default_factory=_StopRequest)

    @property
    def sinks(self) -> _LiveStreamSinks:
        """The per-stream taps the stream spawn attaches.

        Returns
        -------
        _LiveStreamSinks
            The stdout and stderr taps, with the shared queue bound.
        """
        stdout_tap = _QueueTap(self.stdout_queue)
        stderr_tap = _QueueTap(self.stderr_queue)
        return _LiveStreamSinks(
            stdout=_LiveStreamSink(
                stdout_tap.on_chunk,
                self.stdout_queue.note_eof,
                functools.partial(self.fail_live, 0),
                self.stdout_queue.complete,
            ),
            stderr=_LiveStreamSink(
                stderr_tap.on_chunk,
                self.stderr_queue.note_eof,
                functools.partial(self.fail_live, 1),
                self.stderr_queue.complete,
            ),
            max_buffered_bytes=self.stdout_queue.capacity,
            stop=self.stop,
        )

    @property
    def queues(self) -> tuple[_ByteQueue, _ByteQueue]:
        """The two queues, in stream order.

        Returns
        -------
        tuple[_ByteQueue, _ByteQueue]
            The stdout queue, then the stderr queue.
        """
        return self.stdout_queue, self.stderr_queue

    def begin_settlement(self) -> None:
        """Stop live acceptance on both queues and wake blocked producers."""
        for queue in self.queues:
            queue.begin_settlement()

    def finalize_settlement(self) -> None:
        """Publish truncation on each queue that never reached a terminal state."""
        for queue in self.queues:
            queue.finalize_settlement()

    def fail(self, index: int, cause: BaseException) -> None:
        """Publish one stream's failure and begin the other's settlement.

        Parameters
        ----------
        index : int
            ``0`` for stdout, ``1`` for stderr.
        cause : BaseException
            The drain or supervisor failure to chain.
        """
        queues = self.queues
        queues[index].fail(cause)
        for other_index, queue in enumerate(queues):
            if other_index != index:
                queue.begin_settlement()

    def fail_live(self, index: int, cause: BaseException) -> None:
        """Publish a live drain failure and ask for the child to be stopped.

        A failure discovered at reconciliation uses :meth:`fail` alone: the
        child has already ended, so scheduling another stop would add a task
        with nothing to stop.
        """
        self.fail(index, cause)
        self.stop.request()


def build_byte_streams(max_buffered_bytes: int) -> _ByteStreams:
    """Create the queues and readers for one byte run.

    Parameters
    ----------
    max_buffered_bytes : int
        The payload-byte bound for each stream's queue, and the cap a byte
        run applies to each stream's read size.

    Returns
    -------
    _ByteStreams
        The two queues, their readers, and the tap builder.

    Raises
    ------
    TypeError
        If *max_buffered_bytes* is not an integer or is a ``bool``.
    ValueError
        If *max_buffered_bytes* is not positive.

    Examples
    --------
    >>> streams = build_byte_streams(64 * 1024)
    >>> streams.stdout.stream.value
    'stdout'
    """  # ruff: ignore[docstring-extraneous-exception] - the bound validator raises both
    capacity = _validate_buffered_bytes(max_buffered_bytes)
    stdout_queue = _ByteQueue(capacity)
    stderr_queue = _ByteQueue(capacity)
    return _ByteStreams(
        stdout_queue=stdout_queue,
        stderr_queue=stderr_queue,
        stdout=ByteStreamReader(EchoStream.STDOUT, stdout_queue),
        stderr=ByteStreamReader(EchoStream.STDERR, stderr_queue),
    )
