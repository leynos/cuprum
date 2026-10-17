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
from cuprum._stdio_diagnostics import _emit_stdio_error, _StdioFailure
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
#
# ``ENCODER`` is marked here even though the encoder is not a chunk: it is built
# lazily on the first ``str`` chunk, so a mistyped codec fails *inside* a write,
# and ``_write_boundary_category`` reads this mark to name it rather than
# guessing from the exception type.
type _ChunkBoundary = typ.Literal[
    StdioFailureCategory.INVALID_CHUNK,
    StdioFailureCategory.ENCODER,
]


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
    def has_encoder(self) -> bool:
        """Whether text processing has already built this run's encoder.

        The distinction the final flush turns on, and the reason it cannot be
        recovered from the encoder itself: building one to find out is the very
        act that emits a byte-order mark. A byte-only producer never asks for an
        encoder, so this stays ``False`` for its whole run.
        """
        return self._encoder is not None

    @property
    def chunk_error(self) -> _ChunkBoundary | None:
        """Where this run's own encoding or chunk handling failed, if it did."""
        return self._chunk_error

    def mark_invalid_chunk(self) -> None:
        """Record that the producer yielded a chunk cuprum cannot write."""
        self._chunk_error = StdioFailureCategory.INVALID_CHUNK

    def report_boundary(
        self,
        exc: BaseException,
        *,
        category: StdioFailureCategory,
        operation: str,
    ) -> None:
        """Emit the diagnostic for a failure of this run's write side.

        The approach is unary for the reason this class exists: the observation
        and the child are bound once per run and never vary between chunks, so
        the sink already carries them together. A caller names only the two
        things that differ between one failure and the next — which boundary
        this was, and which exception it was.

        Only the *report* lives here. Each caller raises its own source error at
        its own ``raise``, because the linter that requires a raised exception
        to be visible in the function that raises it is also what keeps a
        docstring's ``Raises`` section honest about a private class no caller
        can import.

        Parameters
        ----------
        exc : BaseException
            The failure the diagnostic names. Only its class name is reported;
            the message stays with the caller, per the payload policy.
        category : StdioFailureCategory
            The boundary this failure belongs to.
        operation : str
            The stable operation name for that boundary.
        """
        _emit_stdio_error(
            self.observation,
            _StdioFailure(
                category=category,
                operation=operation,
                error_type=type(exc).__name__,
                pid=self.process.pid,
            ),
        )

    def encode(self, text: str, *, final: bool) -> bytes:
        """Encode *text*, attributing any failure to the encoder boundary.

        The encoder is not only built lazily; it *fails* lazily too. A strict
        codec rejects an unencodable character from inside ``encode()`` rather
        than from inside the constructor, so marking ``ENCODER`` only where the
        encoder is built would let a genuine ``UnicodeEncodeError`` reach the
        classifier unmarked and be reported as ``PIPE`` — a pipe that was never
        at fault. Marking here covers every call that can raise for an encoding
        reason: the lazy build, the per-chunk encode, and the final flush.

        The mark is cleared only on success, so an exception propagates with it
        still set. That is what the classifier reads, and it is why this cannot
        be a plain ``try``/``finally`` restore: the failure case *is* the one
        that needs the mark.

        Parameters
        ----------
        text : str
            The chunk text, or ``""`` for the final flush.
        final : bool
            Whether this is the encoder's final flush, which is the only call
            that emits a held partial sequence.

        Returns
        -------
        bytes
            The encoded payload, empty when the encoder is still holding a
            partial sequence.
        """
        previous = self._chunk_error
        self._chunk_error = StdioFailureCategory.ENCODER
        payload = self.encoder().encode(text, final=final)
        self._chunk_error = previous
        return payload


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
            payload = sink.encode(chunk, final=False)
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
    r"""Write whatever the incremental encoder is still holding.

    A trailing partial sequence only becomes visible to the child here, so this
    runs after the producer is exhausted and before the pipe is closed. A
    stream that never produced text has nothing held and nothing to flush, so
    its encoder is never built; constructing one to discover that is not a
    harmless query. Several codecs emit their byte-order mark from a whole
    encoder's bytes, and they emit it on that encoder's *first* ``encode`` call
    — including ``encode("", final=True)``. Flushing a byte-only stream
    therefore appends a BOM to bytes the caller supplied verbatim: ``utf-8-sig``
    turns ``b"raw"`` into ``b"raw\xef\xbb\xbf"``, and a producer that yields
    nothing sends three bytes instead of end-of-file. An encoding name this
    build does not know fails the run for the same reason, from a codec the
    caller's bytes never needed.

    Only an encoder that already exists is flushed, which is exactly the set of
    runs whose text might still be inside one. The stateful codecs that need a
    final escape sequence on the wire are in that set by construction: the
    escape is owed because a ``str`` chunk was encoded, and encoding one is what
    built the encoder.
    """
    if not sink.has_encoder:
        return
    tail = sink.encode("", final=True)
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
