"""Subprocess stdin writing helpers.

Owns the ``cuprum.stdin`` logger and the stdin pipe lifecycle: writing
caller-provided bytes, closing the pipe, and emitting observable diagnostics
when the pipe fails early.

The two sources differ in how much of the payload exists at once. A payload
(``StdinInput``) is already complete in memory, so it is written in one go;
that writer lives here. A producer (``StdinStream``) is pulled one chunk at a
time and is handled by ``cuprum._subprocess_stdin_stream``, which this module's
dispatcher starts when a caller supplies one. The dispatcher reads a resolved
:class:`~cuprum._stdio_plan._StdinPlan` rather than the caller's raw argument,
so the inherited/payload/producer decision is made once, when the stdio is
resolved, instead of being re-derived here.
"""

from __future__ import annotations

import asyncio
import logging
import typing as typ

from cuprum._pipeline_internals import _EventDetails, _StageObservation
from cuprum._stdio_plan import (
    _NoStdin,
    _PayloadStdin,
    _PipeStdin,
    _StdinPlan,
    _StreamStdin,
)

if typ.TYPE_CHECKING:
    from cuprum._subprocess_stdin_stream import _StdinCodec

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
    stdin_data: bytes,
    observation: _StageObservation,
) -> None:
    """Write caller-provided stdin bytes and close the pipe.

    *stdin_data* is always a real ``bytes`` value: the only caller is
    :func:`_spawn_stdin_writer`, which returns before starting this task for an
    inherited stdin and routes a producer to the streaming writer instead. The
    payload is empty only for a run that explicitly asked for a stdin pipe and
    supplied no source, and writing nothing before closing is exactly the
    child-side view that request describes.
    """
    stdin = process.stdin
    if stdin is None:
        _LOGGER.debug("stdin_writer_skipped pid=%s reason=no_pipe", process.pid)
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


async def _cancel_stdin_writer(stdin_task: asyncio.Task[None] | None) -> None:
    """Cancel and drain a stdin writer task, tolerating any raised error.

    Used on the timeout and cancellation paths to reclaim a writer that may be
    blocked draining bytes into an unread pipe, so its cleanup cannot delay the
    surrounding failure work.
    """
    if stdin_task is None:
        return
    stdin_task.cancel()
    await asyncio.gather(stdin_task, return_exceptions=True)


def _spawn_payload_writer(
    process: asyncio.subprocess.Process,
    payload: bytes,
    observation: _StageObservation,
) -> asyncio.Task[None]:
    """Start the writer that sends one complete payload and closes the pipe.

    Both payload-shaped plan variants land here, and deliberately so: an
    explicitly requested empty stdin pipe and an empty payload are the same
    child-side view, so they share one writer rather than growing a second path
    that expresses the same thing twice.

    Returns
    -------
    asyncio.Task[None]
        The payload writer task, already scheduled.
    """
    _LOGGER.debug(
        "stdin_writer_task_start pid=%s bytes=%s",
        process.pid,
        len(payload),
        extra={"cuprum_pid": process.pid, "cuprum_stdin_bytes": len(payload)},
    )
    return asyncio.create_task(_write_stdin(process, payload, observation))


def _spawn_stdin_writer(
    process: asyncio.subprocess.Process,
    plan: _StdinPlan,
    codec: _StdinCodec,
    observation: _StageObservation,
) -> asyncio.Task[None] | None:
    """Start the stdin writer that a resolved plan calls for, if any.

    The plan decides, and its four variants map onto three outcomes. An
    inherited stdin needs no task: there is no pipe of cuprum's to write to or
    close. A producer gets the streaming writer, which owns its own backpressure
    and finalization. A payload — including the empty one that
    :class:`~cuprum._stdio_plan._PipeStdin` reduces to — goes to the payload
    writer, which writes what it was given and closes the pipe. Reusing that
    writer for an explicitly requested empty pipe is deliberate: the variant was
    reached by asking for a pipe and supplying no source, so the child should
    see exactly what an empty payload gives it, without a second code path
    expressing the same thing.

    Returns
    -------
    asyncio.Task[None] | None
        The writer task, or ``None`` for an inherited stdin, which has no pipe
        of cuprum's to write to or close.
    """
    # The streaming writer imports this module back for the pipe primitives, so
    # a module-level import of it would close a cycle at load time, when neither
    # module is complete. Deferring the import also keeps the name looked up on
    # the module, which is where the test suite's monkeypatch seams replace it.
    from cuprum._subprocess_stdin_stream import _write_stdin_stream

    match plan:
        case _NoStdin():
            # An inherited stdin has no pipe of cuprum's, so no writer and
            # nothing to close: the child uses the parent's own descriptor.
            return None
        case _StreamStdin(stream=producer):
            _LOGGER.debug("stdin_writer_task_start pid=%s source=stream", process.pid)
            return asyncio.create_task(
                _write_stdin_stream(process, producer, codec, observation)
            )
        case _PipeStdin():
            # Requested a pipe, supplied no source: the child should see exactly
            # what an empty payload gives it, so the payload writer is reused
            # rather than a second code path expressing the same thing.
            return _spawn_payload_writer(process, b"", observation)
        case _PayloadStdin(data=payload):
            return _spawn_payload_writer(process, payload, observation)


__all__ = [
    "_cancel_stdin_writer",
    "_close_stdin",
    "_emit_stdin_error",
    "_spawn_stdin_writer",
    "_write_stdin",
]
