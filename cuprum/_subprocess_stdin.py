"""Subprocess stdin writing helpers.

Owns the ``cuprum.stdin`` logger and the stdin pipe lifecycle: writing
caller-provided bytes or pulling an async producer's chunks, closing the pipe,
and emitting observable diagnostics when the pipe fails early.

The two sources differ in how much of the payload exists at once. A payload
(``StdinInput``) is already complete in memory, so it is written in one go. A
producer (``StdinStream``) is pulled one chunk at a time, and each chunk is
drained into the child before the next is pulled, so the parent's peak
retention is the largest chunk rather than the whole stream. Pulling after the
drain is what makes the bound real: pulling eagerly would let an unbounded
producer outrun the child and re-materialize the payload in the parent's
memory.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import dataclasses as dc
import logging
import typing as typ

from cuprum._pipeline_internals import _EventDetails, _StageObservation

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh.execution import StdinStream

_LOGGER = logging.getLogger("cuprum.stdin")


def _emit_stdin_error(
    process: asyncio.subprocess.Process,
    observation: _StageObservation,
    exc: BaseException,
    *,
    operation: str,
) -> None:
    """Emit an observable diagnostic for stdin pipe write failures."""
    _LOGGER.error(
        "stdin_%s_failed pid=%s error=%s",
        operation,
        process.pid,
        type(exc).__name__,
        exc_info=exc,
        extra={
            "cuprum_pid": process.pid,
            "cuprum_stdin_operation": operation,
            "cuprum_error_type": type(exc).__name__,
        },
    )
    observation.emit(
        "stdin_error",
        _EventDetails(
            pid=process.pid,
            operation=operation,
            error_type=type(exc).__name__,
            note=f"{type(exc).__name__}: {exc!s}",
        ),
    )


async def _write_stdin(
    process: asyncio.subprocess.Process,
    stdin_data: bytes | None,
    observation: _StageObservation,
) -> None:
    """Write caller-provided stdin bytes and close the pipe."""
    stdin = process.stdin
    if stdin is None:
        _LOGGER.debug("stdin_writer_skipped pid=%s reason=no_pipe", process.pid)
        return
    if stdin_data is None:
        # A stream producer reaching this writer means the spawn layer bound
        # stdin to a pipe without planning to pull it. Closing immediately is
        # the safe fallback: the child sees EOF rather than hanging forever on
        # a pipe nobody will write to.
        await _close_stdin(process, stdin, observation)
        return
    _LOGGER.debug(
        "stdin_writer_write_start pid=%s bytes=%s",
        process.pid,
        len(stdin_data),
        extra={"cuprum_pid": process.pid, "cuprum_stdin_bytes": len(stdin_data)},
    )
    try:
        stdin.write(stdin_data)
        await stdin.drain()
        observation.emit(
            "stdin",
            _EventDetails(pid=process.pid, byte_count=len(stdin_data)),
        )
    except (OSError, RuntimeError) as exc:
        _emit_stdin_error(process, observation, exc, operation="write")
    finally:
        await _close_stdin(process, stdin, observation)
    _LOGGER.debug("stdin_writer_finished pid=%s", process.pid)


async def _close_stdin(
    process: asyncio.subprocess.Process,
    stdin: asyncio.StreamWriter,
    observation: _StageObservation,
) -> None:
    """Close the child's stdin pipe, reporting any failure to close it."""
    try:
        _LOGGER.debug("stdin_writer_close_start pid=%s", process.pid)
        stdin.close()
        await stdin.wait_closed()
    except (OSError, RuntimeError) as exc:
        _emit_stdin_error(process, observation, exc, operation="close")


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
    observation: _StageObservation,
) -> None:
    """Pull *stream*'s chunks and write each to the child before the next.

    The encoder is incremental, so a multi-byte character split across two
    ``str`` chunks is still encoded correctly: the encoder holds the partial
    byte sequence until the next chunk completes it. A final ``flush`` emits
    whatever the encoder is still holding, which is the only way a trailing
    partial sequence becomes visible to the child.

    An early child-side close (``BrokenPipeError``, or an ``EPIPE``-backed
    ``OSError``) is *not* an error: a child that reads only part of its input
    and exits is behaving normally, so the condition is recorded as a
    ``stdin_error`` observation and the run proceeds to its exit code. A
    producer or encoder failure is different: it is wrapped in
    ``StdinSourceError`` and raised, because the run's input contract was
    broken rather than satisfied early.

    The pipe is closed and the producer finalized on every exit path,
    including the raising ones, so a caller who sees a failure still knows
    that nothing cuprum owns outlives the run.

    Raises
    ------
    _source_error
        If pulling or encoding a chunk fails. The helper builds the public
        ``StdinSourceError`` from the lazy shim, so what a caller catches is
        that type, with the producer's own exception chained as ``__cause__``.
        It is raised through the helper rather than named literally here
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
    encoder = codecs.getincrementalencoder(getattr(process, "encoding", "utf-8"))(
        getattr(process, "errors", "replace")
    )
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
    except (BrokenPipeError, ConnectionResetError) as exc:
        _emit_stdin_error(process, observation, exc, operation="early_close")
    except Exception as exc:
        msg = f"stdin producer failed: {type(exc).__name__}: {exc!s}"
        raise _source_error(msg, exc) from exc
    finally:
        await _finalize_stdin_source(source)
        await _close_stdin(process, stdin, observation)
    _LOGGER.debug("stdin_writer_finished pid=%s", process.pid)


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


async def _cancel_stdin_writer(stdin_task: asyncio.Task[None] | None) -> None:
    """Cancel and drain a stdin writer task, tolerating any raised error.

    Used on the timeout and cancellation paths to reclaim a writer that may be
    blocked draining bytes into an unread pipe, so its cleanup cannot delay the
    surrounding failure handling.
    """
    if stdin_task is None:
        return
    stdin_task.cancel()
    await asyncio.gather(stdin_task, return_exceptions=True)


def _spawn_stdin_writer(
    process: asyncio.subprocess.Process,
    stdin_data: bytes | StdinStream | None,
    observation: _StageObservation,
) -> asyncio.Task[None] | None:
    """Start stdin writing when a payload or a producer was supplied."""
    # Imported here rather than at module load, for the same reason
    # :func:`_source_error` reaches its type through the lazy shim:
    # ``cuprum.sh.execution`` is complete only once the ``cuprum`` surface
    # that imports this module has finished initializing. Naming the class
    # locally also lets the type checker narrow the union at the ``isinstance``
    # below, which is what keeps the payload branch free of a redundant guard.
    from cuprum.sh.execution import StdinStream

    if stdin_data is None:
        return None
    if isinstance(stdin_data, StdinStream):
        _LOGGER.debug("stdin_writer_task_start pid=%s source=stream", process.pid)
        return asyncio.create_task(
            _write_stdin_stream(process, stdin_data, observation)
        )
    _LOGGER.debug(
        "stdin_writer_task_start pid=%s bytes=%s",
        process.pid,
        len(stdin_data),
        extra={"cuprum_pid": process.pid, "cuprum_stdin_bytes": len(stdin_data)},
    )
    return asyncio.create_task(_write_stdin(process, stdin_data, observation))


__all__ = [
    "_cancel_stdin_writer",
    "_emit_stdin_error",
    "_spawn_stdin_writer",
    "_write_stdin",
    "_write_stdin_stream",
]
