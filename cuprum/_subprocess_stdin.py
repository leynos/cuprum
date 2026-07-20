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


def _emit_stdin_error(
    process: asyncio.subprocess.Process,
    observation: _StageObservation,
    exc: BaseException,
    *,
    operation: str,
) -> None:
    """Emit an observable diagnostic for stdin pipe write failures."""
    _LOGGER.warning(
        "stdin_%s_failed pid=%s error=%s: %s",
        operation,
        process.pid,
        type(exc).__name__,
        exc,
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
        try:
            _LOGGER.debug("stdin_writer_close_start pid=%s", process.pid)
            stdin.close()
            await stdin.wait_closed()
        except (OSError, RuntimeError) as exc:
            _emit_stdin_error(process, observation, exc, operation="close")
    _LOGGER.debug("stdin_writer_finished pid=%s", process.pid)


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
