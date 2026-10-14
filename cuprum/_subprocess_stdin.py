"""Subprocess stdin writing helpers.

Owns the ``cuprum.stdin`` logger and the stdin pipe lifecycle: writing
caller-provided bytes, closing the pipe, and emitting observable
diagnostics when the pipe fails early.
"""

from __future__ import annotations

import asyncio
import logging

from cuprum._pipeline_internals import _EventDetails, _StageObservation

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
    """Write caller-provided stdin bytes and close the pipe."""
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
        await _close_stdin(stdin, process, observation)
    _LOGGER.debug("stdin_writer_finished pid=%s", process.pid)


async def _close_stdin(
    stdin: asyncio.StreamWriter,
    process: asyncio.subprocess.Process,
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
    stdin_data: bytes | None,
    observation: _StageObservation,
) -> asyncio.Task[None] | None:
    """Start stdin writing when direct stdin data was supplied."""
    if stdin_data is None:
        return None
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
]
