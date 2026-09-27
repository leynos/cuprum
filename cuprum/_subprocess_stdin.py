"""Subprocess stdin writing helpers.

Owns the ``cuprum.stdin`` logger and the stdin pipe lifecycle: writing
caller-provided bytes, closing the pipe, and emitting observable diagnostics
when the pipe fails early.

The two sources differ in how much of the payload exists at once. A payload
(``StdinInput``) is already complete in memory, so it is written in one go;
that writer lives here. A producer (``StdinStream``) is pulled one chunk at a
time and is handled by ``cuprum._subprocess_stdin_stream``, which this module's
dispatcher starts when a caller supplies one.
"""

from __future__ import annotations

import asyncio
import logging
import typing as typ

from cuprum._pipeline_internals import _EventDetails, _StageObservation

if typ.TYPE_CHECKING:
    from cuprum._subprocess_stdin_stream import _StdinCodec
    from cuprum.sh.execution import StdinStream

_LOGGER = logging.getLogger("cuprum.stdin")

# How long the writer waits for its own transport to report the buffer flushed
# once the run is tearing down. The reader end may never drain — a descendant
# that inherited stdin and does not read it — so this has to be a bound rather
# than a wait; see ``_close_stdin``.
_CLOSE_GRACE_S = 0.5


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

    *stdin_data* is never ``None``: the only caller is
    :func:`_spawn_stdin_writer`, which returns before starting this task when
    no payload was supplied, and routes a ``StdinStream`` to the streaming
    writer instead. The payload is therefore a real ``bytes`` value here, and
    the parameter says so.
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
    """Close the child's stdin without letting the close outlive the writer.

    ``wait_closed`` cannot be allowed to block here. The transport only reports
    its buffer flushed once the *reader* end drains it, and the reader is the
    child or a descendant — so when a descendant inherits stdin and never reads
    it, the buffer never drains and this await never returns. That is harmless
    for a writer running to completion, but this task is cancelled precisely
    when the run is ending, and a ``finally`` that cannot be interrupted turns
    every such cancellation into a hang: the task stays alive, and whoever is
    waiting for it — the deadline's own reconciliation, or the run's writer
    await — waits indefinitely. Observed directly: the task parked at
    ``wait_closed`` with ``cancelling() == 1`` for as long as the probe ran.

    The close is still given a bounded grace, because a transport whose reader
    is still draining should get the chance to flush what it can. What is
    removed is the *unbounded* wait, not the flush. A timeout here is expected
    rather than exceptional: it is the signal that the reader end is still
    open, so it is logged at debug and not reported as a pipe failure.
    """
    _LOGGER.debug("stdin_writer_close_start pid=%s", process.pid)
    try:
        stdin.close()
        async with asyncio.timeout(_CLOSE_GRACE_S):
            await stdin.wait_closed()
    except TimeoutError:
        # Expected when the reader end is still open, and not a pipe failure:
        # ``TimeoutError`` is an ``OSError``, so this arm has to come first or
        # the bound below is reported as a broken pipe.
        _LOGGER.debug(
            "stdin_writer_close_pending pid=%s reason=reader_still_open",
            process.pid,
        )
    except (OSError, RuntimeError) as exc:
        _emit_stdin_error(process, observation, exc, operation="close")


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
    codec: _StdinCodec,
    observation: _StageObservation,
) -> asyncio.Task[None] | None:
    """Start stdin writing when a payload or a producer was supplied."""
    # Both imports are deferred. ``cuprum.sh.execution`` is complete only once
    # the ``cuprum`` surface that imports this module has finished
    # initializing, and the streaming writer imports this module back for the
    # pipe primitives — so a module-level import of either one would close a
    # cycle at load time, when neither module is complete. Naming
    # ``StdinStream`` locally also lets the type checker narrow the union at the
    # ``isinstance`` below, which is what keeps the payload branch free of a
    # redundant guard.
    from cuprum._subprocess_stdin_stream import _write_stdin_stream
    from cuprum.sh.execution import StdinStream

    if stdin_data is None:
        return None
    if isinstance(stdin_data, StdinStream):
        _LOGGER.debug("stdin_writer_task_start pid=%s source=stream", process.pid)
        return asyncio.create_task(
            _write_stdin_stream(process, stdin_data, codec, observation)
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
    "_close_stdin",
    "_emit_stdin_error",
    "_spawn_stdin_writer",
    "_write_stdin",
]
