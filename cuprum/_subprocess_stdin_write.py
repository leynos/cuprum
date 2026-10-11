"""Writing one streaming chunk to the child's stdin, and classifying failures.

Split from ``cuprum._subprocess_stdin_stream`` when the source-failure fix
pushed that module past the 400-line ceiling. The seam is the one the code
already drew: this module owns the encoder, the per-chunk write, and the
question of whether a pipe error came from the child closing its end — all of
which need to know only about a sink, never about producers or error types.
``_subprocess_stdin_stream`` keeps the pull loop and the failure handling that
turns those events into the public outcome.
"""

from __future__ import annotations

import dataclasses as dc
import errno
import typing as typ

from cuprum._pipeline_internals import _EventDetails
from cuprum.stdio_events import StdioFailureCategory

if typ.TYPE_CHECKING:
    import asyncio
    import codecs

    from cuprum._pipeline_internals import _StageObservation
    from cuprum._subprocess_stdin_stream import _StdinCodec

# Which boundary inside the streaming write failed, for the failures this
# module raises itself. ``None`` for a pipe write, because whether that was the
# child closing its end or a genuine pipe fault is a question only the caller's
# classifier can answer.
type _ChunkBoundary = typ.Literal[StdioFailureCategory.INVALID_CHUNK]


@dc.dataclass(slots=True)
class _StreamSink:
    """Everything one streaming write needs beyond the chunk itself.

    These values are bound once per run and never vary between chunks, so they
    travel as one object rather than as several positional arguments. That
    keeps the per-chunk helpers to a single changing parameter, which is what
    makes their signatures readable at the call site.

    The encoder is built on first use rather than up front because building it
    is fallible: an unknown codec name raises ``LookupError``, and a sink that
    demanded an encoder would have to raise that from whichever caller
    constructed it — before the ``try`` that wraps source failures, and so
    outside the region that finalizes the producer. Lazy construction puts
    that failure where every other source failure is already handled.

    :attr:`chunk_error` is how the boundary survives the raise. Advancing the
    producer and the write path both raise through ``_drain_source_into_pipe``,
    and by the time the exception reaches it the two are indistinguishable by
    type — an invalid chunk and a mistyped encoder are both ``TypeError``. The
    write path therefore records the boundary on the sink it raised from, and
    the classifier reads it back rather than guessing from the exception.
    """

    process: asyncio.subprocess.Process
    stdin: asyncio.StreamWriter
    codec: _StdinCodec
    observation: _StageObservation
    _encoder: codecs.IncrementalEncoder | None = None
    _chunk_error: _ChunkBoundary | None = None

    def encoder(self) -> codecs.IncrementalEncoder:
        """Return this run's incremental encoder, building it on first use.

        Returns
        -------
        codecs.IncrementalEncoder
            The encoder ``_write_chunk`` encodes ``str`` chunks with. One is
            built per run and reused, so a multi-byte character split across
            two chunks is still encoded correctly.

        Raises
        ------
        LookupError
            If the run's encoding names no codec this build of Python knows.
        """
        try:
            if self._encoder is None:
                self._encoder = self.codec.encoder()
        except LookupError:
            # Recorded before re-raising so the classifier can tell an unknown
            # codec from an invalid chunk; both reach it as a bare
            # ``LookupError``/``TypeError`` otherwise.
            self._chunk_error = StdioFailureCategory.ENCODER
            raise
        return self._encoder

    @property
    def chunk_error(self) -> _ChunkBoundary | None:
        """Where this run's own encoding or chunk handling failed, if it did."""
        return self._chunk_error

    def mark_invalid_chunk(self) -> None:
        """Record that the producer yielded a chunk cuprum cannot write."""
        self._chunk_error = StdioFailureCategory.INVALID_CHUNK


async def _write_chunk(
    sink: _StreamSink,
    chunk: str | bytes,
) -> None:
    """Encode one chunk, write it, and drain before returning.

    Draining before returning is the backpressure: the caller's next pull
    happens only after ``drain()`` has returned, and because that return comes
    at the transport's low-water mark rather than on an empty buffer, the
    writer may pull ahead of the child's reads. The bound is on how far ahead
    it runs, not on whether the child has taken these bytes off the pipe.

    An empty encoded payload writes nothing and emits nothing: a ``str`` chunk
    that the incremental encoder is still holding entirely (a lone leading
    surrogate, say) produces no bytes, and reporting a zero-byte write would
    be noise.

    Raises
    ------
    TypeError
        If the producer yields anything other than ``str`` or ``bytes``. A
        chunk of the wrong type is a caller error, not a pipe condition, so
        it is raised here for the streaming writer to wrap.
    """
    match chunk:
        case str():
            payload = sink.encoder().encode(chunk, final=False)
        case bytes():
            payload = chunk
        case _:
            sink.mark_invalid_chunk()
            msg = (
                f"stdin producer yielded {type(chunk).__name__}; "
                f"chunks must be str or bytes"
            )
            raise TypeError(msg)
    if not payload:
        return
    sink.stdin.write(payload)
    await sink.stdin.drain()
    sink.observation.emit(
        "stdin",
        _EventDetails(pid=sink.process.pid, byte_count=len(payload)),
    )


async def _flush_encoder(sink: _StreamSink) -> None:
    """Write whatever the incremental encoder is still holding.

    A trailing partial sequence only becomes visible to the child here, so this
    runs after the producer is exhausted and before the pipe is closed.
    """
    tail = sink.encoder().encode("", final=True)
    if not tail:
        return
    sink.stdin.write(tail)
    await sink.stdin.drain()
    sink.observation.emit(
        "stdin",
        _EventDetails(pid=sink.process.pid, byte_count=len(tail)),
    )


def _is_early_close(exc: BaseException) -> bool:
    """Whether *exc* is the child closing its end of the input pipe.

    ``BrokenPipeError`` is how CPython spells a pipe write, but the
    subclassing happens at construction only: an ``OSError`` whose errno is
    assigned afterwards stays plain. Both are the same pipe condition, so both
    belong on the early-close path.

    Parameters
    ----------
    exc : BaseException
        The failure raised while writing to the child's stdin.

    Returns
    -------
    bool
        ``True`` when the child closed the pipe rather than cuprum failing.
    """
    if isinstance(exc, BrokenPipeError | ConnectionResetError):
        return True
    return isinstance(exc, OSError) and exc.errno == errno.EPIPE


__all__ = [
    "_StreamSink",
    "_flush_encoder",
    "_is_early_close",
    "_write_chunk",
]
