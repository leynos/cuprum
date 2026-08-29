"""Pump one pipeline stage's stdout into the next stage's stdin.

The pure-Python home for the writer side of stream handling: ``_pump_stream``
and ``_relay_chunks`` copy chunks between stages with ``writer.drain()``
backpressure, draining to EOF without a writer and best-effort under a bounded
timeout after an early downstream close, which reduces (without eliminating)
the risk of upstream stages blocking or deadlocking.
``_write_to_stream_writer`` reports broken-pipe state as a ``_WriteOutcome``
value, and ``_close_stream_writer`` tears the writer down while swallowing
already-closed-pipe errors. Consumed by ``cuprum._streams`` (which re-exports
this surface) and the pipeline execution layer. ``_pump_stream`` closes any
supplied writer once relay completes or when no reader is supplied, so callers
must not reuse the writer afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses as dc
import enum
import logging
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_READ_SIZE = 65536
_POST_CLOSE_DRAIN_TIMEOUT_S = 0.25
_LOGGER = logging.getLogger(__name__)
_ACTIVE_READ_SIZE: contextvars.ContextVar[int] = contextvars.ContextVar(
    "cuprum_active_read_size",
    default=_READ_SIZE,
)


def _current_read_size() -> int:
    """Return the task-local read size active for this execution."""
    return _ACTIVE_READ_SIZE.get()


@contextlib.contextmanager
def _override_read_size(read_size: int) -> cabc.Iterator[None]:
    """Use ``read_size`` for one benchmark worker without shared mutation."""
    token = _ACTIVE_READ_SIZE.set(read_size)
    try:
        yield
    finally:
        _ACTIVE_READ_SIZE.reset(token)


@dc.dataclass(slots=True)
class _DrainProgress:
    """Track bytes discarded by a cancellable reader drain."""

    discarded_bytes: int = 0


class _WriteOutcome(enum.Enum):
    """Whether a downstream writer is still accepting data after a write.

    Used by :func:`_write_to_stream_writer` to report broken-pipe state as a
    value rather than overloading a ``StreamWriter | None`` return, so the
    caller retains ownership of the writer and decides when to close it.

    Members
    -------
    OPEN
        The write succeeded and the downstream writer remains open.
    CLOSED
        The downstream closed early (broken pipe); stop writing to it.
    """

    OPEN = "open"
    CLOSED = "closed"


async def _pump_stream(
    reader: asyncio.StreamReader | None,
    writer: asyncio.StreamWriter | None,
    *,
    read_size: int = _READ_SIZE,
) -> None:
    """Stream stdout into stdin with backpressure via ``drain``.

    When the downstream stdin closes early (for example because the next stage
    terminates), this helper continues draining stdout to avoid deadlocking
    upstream stages.
    """
    if reader is None:
        await _close_stream_writer(writer)
        return

    try:
        await _relay_chunks(reader, writer, read_size=read_size)
    finally:
        _LOGGER.debug("stream_writer_close_start")
        await _close_stream_writer(writer)


async def _relay_chunks(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
    *,
    read_size: int = _READ_SIZE,
) -> None:
    """Copy chunks downstream; drain to EOF with no writer, bounded after a close."""
    if writer is None:
        await _drain_stream_reader(reader, _DrainProgress(), read_size=read_size)
        return
    while True:
        chunk = await reader.read(read_size)
        if not chunk:
            return
        if await _write_to_stream_writer(writer, chunk) is _WriteOutcome.CLOSED:
            break
    discarded_bytes = await _drain_stream_reader_bounded(reader, read_size=read_size)
    _LOGGER.debug(
        "stream_downstream_closed discarded_bytes=%s",
        discarded_bytes,
        extra={"cuprum_discarded_bytes": discarded_bytes},
    )


async def _drain_stream_reader(
    reader: asyncio.StreamReader,
    progress: _DrainProgress,
    *,
    read_size: int = _READ_SIZE,
) -> int:
    """Consume the reader to EOF, discarding the data and returning byte count."""
    while chunk := await reader.read(read_size):
        progress.discarded_bytes += len(chunk)
    return progress.discarded_bytes


async def _drain_stream_reader_bounded(
    reader: asyncio.StreamReader,
    *,
    read_size: int = _READ_SIZE,
) -> int:
    """Best-effort drain after downstream closure without waiting forever."""
    progress = _DrainProgress()
    try:
        return await asyncio.wait_for(
            _drain_stream_reader(reader, progress, read_size=read_size),
            timeout=_POST_CLOSE_DRAIN_TIMEOUT_S,
        )
    except TimeoutError:
        _LOGGER.debug(
            "stream_reader_drain_timeout timeout_s=%s",
            _POST_CLOSE_DRAIN_TIMEOUT_S,
            extra={"cuprum_timeout_s": _POST_CLOSE_DRAIN_TIMEOUT_S},
        )
        return progress.discarded_bytes


async def _write_to_stream_writer(
    writer: asyncio.StreamWriter,
    chunk: bytes,
) -> _WriteOutcome:
    """Write a chunk; report whether the caller-owned downstream writer stays open."""
    try:
        writer.write(chunk)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        _LOGGER.debug(
            "stream_write_closed bytes=%s",
            len(chunk),
            extra={"cuprum_attempted_bytes": len(chunk)},
        )
        return _WriteOutcome.CLOSED
    return _WriteOutcome.OPEN


async def _close_stream_writer(writer: asyncio.StreamWriter | None) -> None:
    """Close a writer, swallowing errors from already-closed pipes."""
    if writer is None:
        return
    try:
        writer.write_eof()
    except (
        AttributeError,
        NotImplementedError,
        BrokenPipeError,
        ConnectionResetError,
    ) as exc:
        _log_suppressed_stream_close_error("write_eof", exc)
    try:
        writer.close()
    except (BrokenPipeError, ConnectionResetError) as exc:
        _log_suppressed_stream_close_error("close", exc)
        return
    wait_closed = getattr(writer, "wait_closed", None)
    if wait_closed is None:
        return
    try:
        await wait_closed()
    except (
        AttributeError,
        NotImplementedError,
        BrokenPipeError,
        ConnectionResetError,
    ) as exc:
        _log_suppressed_stream_close_error("wait_closed", exc)


def _log_suppressed_stream_close_error(operation: str, exc: BaseException) -> None:
    """Log a cleanup error that cannot safely be raised during pipe teardown."""
    _LOGGER.debug(
        "stream_writer_close_suppressed operation=%s error=%s",
        operation,
        type(exc).__name__,
        exc_info=(type(exc), exc, exc.__traceback__),
        extra={
            "cuprum_operation": operation,
            "cuprum_error_type": type(exc).__name__,
        },
    )


__all__ = [
    "_POST_CLOSE_DRAIN_TIMEOUT_S",
    "_READ_SIZE",
    "_DrainProgress",
    "_WriteOutcome",
    "_close_stream_writer",
    "_current_read_size",
    "_drain_stream_reader",
    "_drain_stream_reader_bounded",
    "_log_suppressed_stream_close_error",
    "_pump_stream",
    "_relay_chunks",
    "_write_to_stream_writer",
]
