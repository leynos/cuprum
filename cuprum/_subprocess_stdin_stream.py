"""Streaming stdin: pull a producer's chunks into the child's pipe.

Split from ``cuprum._subprocess_stdin`` when the streaming writer's encoder,
per-chunk helpers, and source-error construction pushed that module past the
400-line ceiling. The seam is the one its docstring already drew: a complete
payload is written in one go by ``cuprum._subprocess_stdin``, while a producer
(``StdinStream``) is pulled one chunk at a time and is handled here.

The pull-after-drain bound lives here. Each chunk is drained into the child
before the next is pulled, so the writer cannot run far ahead of the child;
pulling eagerly would let an unbounded producer outrun the child and
re-materialize the payload in the parent's memory. What that bounds is how far
*ahead* the producer runs, not what one step retains: ``drain()`` returns once
the transport's write buffer has fallen below its low-water mark, and the OS
pipe holds bytes of its own, so several chunks can be in flight together, and
the chunk currently being written stays live in the parent until its drain
completes. Retained memory is therefore the largest chunk yielded plus those
buffers. The bound is on how far ahead the producer runs; the type does not
police chunk size at all.

This module imports the pipe primitives (``_close_stdin``, ``_emit_stdin_error``)
from ``cuprum._subprocess_stdin``, and the dispatcher in that module imports
this one's ``_write_stdin_stream`` lazily inside the function body. The
dependency therefore only ever runs in one direction once both modules are
loaded.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import dataclasses as dc
import errno
import logging
import typing as typ

from cuprum._pipeline_internals import _EventDetails, _StageObservation
from cuprum._subprocess_stdin import _close_stdin, _emit_stdin_error

if typ.TYPE_CHECKING:
    import collections.abc as cabc

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


@dc.dataclass(frozen=True, slots=True)
class _StreamSink:
    """Everything one streaming write needs beyond the chunk itself.

    These four values are bound once per run and never vary between chunks, so
    they travel as one object rather than as four positional arguments. That
    keeps the per-chunk helpers to a single changing parameter, which is what
    makes their signatures readable at the call site.
    """

    process: asyncio.subprocess.Process
    stdin: asyncio.StreamWriter
    encoder: codecs.IncrementalEncoder
    observation: _StageObservation


async def _write_chunk(
    sink: _StreamSink,
    chunk: str | bytes,
) -> None:
    """Encode one chunk, write it, and drain before returning.

    Draining before returning is the backpressure: the caller's next pull
    happens only once the child has taken this chunk off the pipe.

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
            payload = sink.encoder.encode(chunk, final=False)
        case bytes():
            payload = chunk
        case _:
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
    tail = sink.encoder.encode("", final=True)
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
    encoder = codec.encoder()
    sink = _StreamSink(
        process=process,
        stdin=stdin,
        encoder=encoder,
        observation=observation,
    )
    try:
        source = aiter(stream.chunks)
        async for chunk in source:
            await _write_chunk(sink, chunk)
        await _flush_encoder(sink)
    except asyncio.CancelledError:
        # Cancellation is control flow, not a source failure: the run is being
        # torn down and the caller must see the cancellation, not an error.
        raise
    except Exception as exc:
        if not _is_early_close(exc):
            raise _stdin_source_error(exc) from exc
        _emit_stdin_error(process, observation, exc, operation="early_close")
    finally:
        await _finalize_stdin_source(source)
        await _close_stdin(process, stdin, observation)
    _LOGGER.debug("stdin_writer_finished pid=%s", process.pid)


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
