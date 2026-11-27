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

import dataclasses as dc

from cuprum._stream_tap import _StreamTap as _LiveStreamSink

__all__ = ["_LiveStreamSink", "_LiveStreamSinks"]


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
