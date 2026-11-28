"""The per-stream live taps attached to a run's ordinary drains.

A byte run and a text run share one stream consumer per pipe. The difference is
that a byte run also hands each consumer a tap: an asynchronous ``on_chunk``
observer and a synchronous ``on_eof`` observer. Keeping the tap's shape in this
leaf module lets the stream-spawn layer attach it without importing the byte
package, so the dependency only points from the feature to the core.

The read-size cap belongs here too. A live queue's bound is also the most a
single normal read should reserve, so :meth:`_LiveStreamSinks.read_size_for`
answers the value the stream config is built with. The cap is a sizing choice,
not a correctness one: the queue still splits any chunk larger than its bound.
"""

import asyncio
import collections.abc as cabc
import dataclasses as dc

from cuprum._stream_tap import _StreamTap as _LiveStreamSink

__all__ = ["_LiveStreamSink", "_LiveStreamSinks", "_StopRequest"]


@dc.dataclass(slots=True)
class _StopRequest:
    """The stop a live I/O failure asks for, and the task that runs it.

    A reader or writer that fails while the handle is merely held has not
    asked for an outcome, so nothing would otherwise stop the child. The first
    failure records a request; once the handle binds its stop callable, the
    request becomes one task. Settlement awaits that task before reconciling,
    so the child is always stopped and reaped exactly once.
    """

    callback: cabc.Callable[[], cabc.Awaitable[None]] | None = None
    requested: bool = False
    task: asyncio.Task[None] | None = None
    #: The failure that requested the stop, retained so settlement can raise
    #: it even when the stop makes the child exit first.
    failure: BaseException | None = None

    def bind(self, callback: cabc.Callable[[], cabc.Awaitable[None]]) -> None:
        """Bind the stop callable, honouring a request made before binding."""
        self.callback = callback
        if self.requested:
            self._start()

    def request(self, failure: BaseException | None = None) -> None:
        """Record that a live failure wants the child stopped.

        Parameters
        ----------
        failure : BaseException | None, default=None
            The failure that caused the request, retained for settlement. The
            first failure wins, so a later request cannot replace the cause.
        """
        self.requested = True
        if self.failure is None and failure is not None:
            self.failure = failure
        if self.callback is not None:
            self._start()

    async def await_stop(self) -> None:
        """Wait for a scheduled stop to finish, if one was scheduled."""
        task = self.task
        if task is not None:
            await asyncio.shield(task)

    def _start(self) -> None:
        """Start the bound stop exactly once."""
        callback = self.callback
        if self.task is None and callback is not None:
            self.task = asyncio.ensure_future(callback())


@dc.dataclass(frozen=True, slots=True)
class _LiveStreamSinks:
    """Both streams' live observers, plus the bound they share.

    ``max_buffered_bytes`` is also the cap applied to each stream's read size,
    so a normal read fits one queue reservation. It is carried here rather than
    read from a queue so the stream-spawn layer needs no queue at all.
    """

    stdout: _LiveStreamSink
    stderr: _LiveStreamSink
    max_buffered_bytes: int
    stop: _StopRequest

    def read_size_for(self, configured: int) -> int:
        """Return the read size a byte run should use for one stream.

        Parameters
        ----------
        configured : int
            The profiled or default read size the run would otherwise use.

        Returns
        -------
        int
            ``min(configured, max_buffered_bytes)``, so a normal read fits the
            queue's reservation without being split.
        """
        return min(configured, self.max_buffered_bytes)
