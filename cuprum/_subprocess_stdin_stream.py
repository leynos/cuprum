"""Streaming stdin: pull a producer's chunks into the child's pipe.

Split from ``cuprum._subprocess_stdin`` when the streaming writer's encoder,
per-chunk helpers, and source-error construction pushed that module past the
400-line ceiling. The seam is the one its docstring already drew: a complete
payload is written in one go by ``cuprum._subprocess_stdin``, while a producer
(``StdinStream``) is pulled one chunk at a time and is handled here.

The pull-after-drain bound lives here. Each chunk is drained out of the
parent's transport buffer before the next is pulled, so the writer cannot
run far ahead of the child; pulling eagerly would let an unbounded producer
outrun the child and re-materialize the payload in the parent's memory.
What that bounds is how far *ahead* the producer runs, not what one step
retains: ``drain()`` returns once the transport's write buffer has fallen
below its low-water mark, and the OS pipe holds bytes of its own, so several
chunks can be in flight together, and the chunk currently being written
stays live in the parent until its drain completes. Retained memory is
therefore the largest chunk yielded plus those buffers. The bound is on how
far ahead the producer runs; the type does not police chunk size at all.

This module imports the pipe primitives (``_close_stdin``, ``_emit_stdin_error``)
from ``cuprum._subprocess_stdin``, and the dispatcher in that module imports
this one's ``_write_stdin_stream`` lazily inside the function body. The
dependency therefore only ever runs in one direction once both modules are
loaded.

The per-chunk write machinery — the sink, the encoder, and the question of
whether a pipe error came from the child — lives in
``cuprum._subprocess_stdin_write``, which this module pulls in as ``_write``.
That split keeps each module inside the 400-line ceiling and draws the same
line the code does: this module owns *which* failures are the producer's, and
``_subprocess_stdin_write`` owns *how* a chunk reaches the pipe.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import dataclasses as dc
import logging
import typing as typ

from cuprum import _subprocess_stdin_write as _write
from cuprum._subprocess_stdin import _close_stdin, _emit_stdin_error

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_internals import _StageObservation
    from cuprum.sh.execution import ExecutionContext, StdinStream

_LOGGER = logging.getLogger("cuprum.stdin")


@dc.dataclass(frozen=True, slots=True)
class _StdinCodec:
    """The encoding settings a streaming ``str`` chunk is written with.

    These live in the run's :class:`~cuprum.sh.execution.ExecutionContext`,
    not on the process: ``asyncio.subprocess.Process`` exposes neither an
    ``encoding`` nor an ``errors`` attribute, because cuprum passes raw
    descriptors rather than the text-mode file objects those attributes would
    describe. Reading them off the process therefore produced the fallbacks
    every time and silently ignored the caller's context.
    """

    encoding: str
    errors: str

    def encoder(self) -> codecs.IncrementalEncoder:
        """Build the incremental encoder for this run's text encoding.

        Returns
        -------
        codecs.IncrementalEncoder
            An encoder that buffers a multi-byte character split across two
            ``str`` chunks instead of failing on the incomplete sequence.
        """
        return codecs.getincrementalencoder(self.encoding)(self.errors)


def _stdin_codec(ctx: ExecutionContext) -> _StdinCodec:
    """Read the streaming encoder settings off a run's context.

    The callers hold an execution bundle whose ``ctx`` this reads, so the
    bundle's own shape need not be imported here: every spawn path already
    has an ``ExecutionContext`` in hand.

    Parameters
    ----------
    ctx : ExecutionContext
        The run's execution context.

    Returns
    -------
    _StdinCodec
        The encoding and error handling a streaming ``str`` chunk is written
        with.
    """
    return _StdinCodec(encoding=ctx.encoding, errors=ctx.errors)


async def _finalize_stdin_source(
    source: cabc.AsyncIterator[str | bytes] | None,
) -> None:
    """Close a producer's iterator, tolerating one without ``aclose``.

    A caller may hand cuprum an async *iterable* whose ``__aiter__`` returns a
    generator, or an object whose iterator has no ``aclose`` at all. Only the
    generator protocol's clean-up is attempted, and a failure to finalize a
    producer cuprum no longer needs must not mask the run's real outcome.
    """
    if source is None:
        return
    aclose = getattr(source, "aclose", None)
    if aclose is None:
        return
    with contextlib.suppress(Exception):
        await aclose()


async def _write_stdin_stream(
    process: asyncio.subprocess.Process,
    stream: StdinStream,
    codec: _StdinCodec,
    observation: _StageObservation,
) -> None:
    """Pull *stream*'s chunks and write each to the child before the next.

    The encoder is incremental, so a multi-byte character split across two
    ``str`` chunks is still encoded correctly: the encoder holds the partial
    byte sequence until the next chunk completes it. A final ``flush`` emits
    whatever the encoder is still holding, which is the only way a trailing
    partial sequence becomes visible to the child.

    An early child-side close (``BrokenPipeError``, or a bare ``OSError``
    carrying ``errno.EPIPE``, as the docstring above advertises) is *not* an
    error: a child that reads only part of its input and exits is behaving
    normally, so the condition is recorded as a ``stdin_error`` observation
    and the run proceeds to its exit code. A producer or encoder failure is
    different: it is wrapped in ``StdinSourceError`` and raised, because the
    run's input contract was broken rather than satisfied early.

    The pipe is closed and the producer finalized on every exit path,
    including the raising ones, so a caller who sees a failure still knows
    that nothing cuprum owns outlives the run.

    Raises
    ------
    _stdin_source_error
        If pulling or encoding a chunk fails. The helper builds the public
        ``StdinSourceError`` from the lazy shim, so what a caller catches is
        that type, with the producer's own exception chained as ``__cause__``.
        It is built through the helper rather than named literally here
        because this module must not import ``cuprum.sh`` at runtime.
    asyncio.CancelledError
        If the writer is cancelled while the run is being torn down.
        Cancellation is control flow rather than a source failure, so it
        propagates unchanged instead of being wrapped.
    """
    stdin = process.stdin
    if stdin is None:
        _LOGGER.debug("stdin_writer_skipped pid=%s reason=no_pipe", process.pid)
        return
    source: cabc.AsyncIterator[str | bytes] | None = None
    sink = _write._StreamSink(
        process=process,
        stdin=stdin,
        codec=codec,
        observation=observation,
    )
    try:
        source = aiter(stream.chunks)
        await _pump_chunks(source, sink)
        await _write._flush_encoder(sink)
    except asyncio.CancelledError:
        # Cancellation is control flow, not a source failure: the run is being
        # torn down and the caller must see the cancellation, not an error.
        raise
    except _ProducerFailureError as exc:
        # The producer failed, so there is no child-side close to weigh: the
        # marker is the only reason this is distinguishable once the exception
        # has left _pump_chunks.
        raise _stdin_source_error(exc.cause) from exc.cause
    except Exception as exc:
        if not _write._is_early_close(exc):
            raise _stdin_source_error(exc) from exc
        _emit_stdin_error(process, observation, exc, operation="early_close")
    finally:
        await _finalize_stdin_source(source)
        await _close_stdin(process, stdin, observation)
    _LOGGER.debug("stdin_writer_finished pid=%s", process.pid)


class _ProducerFailureError(Exception):
    """Marks a failure that came from advancing the producer.

    It carries no behaviour, only provenance. Advancing the producer and
    writing to the child both raise ``OSError`` in the pipe family, so once
    control reaches the shared handler the exception type alone cannot say
    which side failed; the marker is what preserves that. The original
    exception travels as :attr:`cause` so the handler can chain it to the
    public error it builds.
    """

    def __init__(self, cause: Exception) -> None:
        """Wrap ``cause`` so the handler can chain it to the public error."""
        super().__init__(str(cause))
        self.cause = cause


async def _pump_chunks(
    source: cabc.AsyncIterator[str | bytes],
    sink: _write._StreamSink,
) -> None:
    """Pull each chunk and write it, keeping the two failure sources apart.

    Advancing the producer and writing to the child both raise ``OSError`` in
    the pipe family when the pipe breaks, but only the write means the child
    closed its end. A producer whose own machinery raises ``BrokenPipeError``
    — reading a socket it owns, say — is the producer failing, and the
    documented contract is that a producer failure becomes a
    ``StdinSourceError`` rather than being read as the child finishing early.

    Wrapping only the producer's ``__anext__`` is what separates them: a pipe
    error raised there can only have come from the producer, so it is marked
    as a producer failure, while the same error from ``_write_chunk``
    propagates unchanged to the early-close handler.

    Parameters
    ----------
    source : collections.abc.AsyncIterator[str | bytes]
        The producer's iterator, advanced one chunk at a time.
    sink : cuprum._subprocess_stdin_write._StreamSink
        The writer, encoder, and observation this run writes through.

    Raises
    ------
    _ProducerFailureError
        If advancing the producer fails for any reason other than the producer
        ending cleanly. The original exception is carried as ``cause``.
    asyncio.CancelledError
        If the producer or the run is cancelled. Cancellation is control flow
        rather than a producer failure, so it propagates unchanged instead of
        being marked.
    """
    while True:
        try:
            chunk = await anext(source)
        except StopAsyncIteration:
            return
        except asyncio.CancelledError:
            # Cancellation is control flow, not a producer failure, so it is
            # never marked here; the caller re-raises it untouched.
            raise
        except Exception as exc:
            raise _ProducerFailureError(exc) from exc
        await _write._write_chunk(sink, chunk)


def _stdin_source_error(exc: BaseException) -> Exception:
    """Build the public ``StdinSourceError`` for a producer or encoder failure.

    Returned rather than raised so each handler can spell ``raise ... from``
    itself: the repository's linter requires the raise to be visible in the
    handler body, and a helper that raised on the caller's behalf would hide
    it.

    Parameters
    ----------
    exc : BaseException
        The producer's or encoder's original failure.

    Returns
    -------
    Exception
        An instance of the public ``StdinSourceError``, ready to raise with
        *exc* chained as ``__cause__``.
    """
    msg = f"stdin producer failed: {type(exc).__name__}: {exc!s}"
    return _source_error(msg, exc)


def _source_error(msg: str, exc: BaseException) -> Exception:
    """Build a ``StdinSourceError`` from its defining module.

    The type lives in ``cuprum.sh.execution``, which this module must not
    import at runtime: ``cuprum.sh`` pulls in the whole ``cuprum`` surface,
    and interior modules are loaded while that surface is still being built.
    The defining module is reached through the same lazy shim the rest of the
    execution layer uses, so the exception the caller catches is the class
    that ``cuprum`` exports.

    Parameters
    ----------
    msg : str
        The message for the raised error.
    exc : BaseException
        The producer's original exception, used only for the fallback path.

    Returns
    -------
    Exception
        An instance of the public ``StdinSourceError``.

    Raises
    ------
    RuntimeError
        If the shim cannot resolve the public type at all, which means the
        cuprum surface is broken rather than the caller's producer.
    """
    from cuprum._subprocess_context import _sh_module

    sh_module = _sh_module()
    error_type = getattr(sh_module, "StdinSourceError", None)
    if error_type is None:
        msg = "cuprum.sh.StdinSourceError is unavailable"
        raise RuntimeError(msg) from exc
    return error_type(msg)


__all__ = [
    "_StdinCodec",
    "_finalize_stdin_source",
    "_stdin_codec",
    "_write_stdin_stream",
]
