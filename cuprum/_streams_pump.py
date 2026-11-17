"""Pump one pipeline stage's stdout into the next stage's stdin.

The pure-Python home for the writer side of stream handling: ``_pump_stream``
and ``_relay_chunks`` copy chunks between stages with ``writer.drain()``
backpressure, draining to EOF without a writer and best-effort under a bounded
timeout after an early downstream close.
``_write_to_stream_writer`` reports broken-pipe state as a ``_WriteOutcome``
value, and ``_close_stream_writer`` tears the writer down while swallowing
already-closed-pipe errors.

A bounded drain that times out is the one case where draining the reader is not
enough. The reader is still attached to a pipe nobody will read again, so a
producer that keeps writing fills the pipe buffer and blocks indefinitely while
pipeline completion waits for every stage. ``_release_stream_reader_transport``
closes that reader transport, which hands the producer a broken pipe so its own
write fails and it exits, keeping cleanup bounded.

Consumed by ``cuprum._streams`` (which re-exports this surface) and the pipeline
execution layer. ``_pump_stream`` closes any supplied writer once relay
completes or when no reader is supplied, so callers must not reuse the writer
afterwards; a reader whose bounded post-close drain timed out is closed too, so
callers must not reuse that reader either.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses as dc
import enum
import logging
import typing as typ

from cuprum._stream_transport import _stream_transport
from cuprum.pump_observation import _current_pump_event_exec_id
from cuprum.stream_events import StreamOperation, StreamOperationOutcome
from cuprum.stream_observation import (
    _complete_stream_operation,
    _record_stream_read,
    _start_stream_operation,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.stream_observation import _StreamOperationMeasurement

_READ_SIZE = 65536
_POST_CLOSE_DRAIN_TIMEOUT_S = 0.25
_LOGGER = logging.getLogger(__name__)
# One event name per direction, so an operator can tell a writer teardown from
# the reader release that makes a still-writing producer fail.
_WRITER_CLOSE_EVENT = "stream_writer_close_suppressed"
_READER_RELEASE_EVENT = "stream_reader_release_suppressed"
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
    measurement: _StreamOperationMeasurement | None = None


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

    Raises
    ------
    asyncio.CancelledError
        If the active task is cancelled while pumping.
    """
    measurement = _start_stream_operation(
        StreamOperation.PIPELINE_TRANSFER,
        exec_id=_current_pump_event_exec_id(),
    )
    outcome = StreamOperationOutcome.EOF

    try:
        try:
            if reader is not None:
                outcome = await _relay_chunks(
                    reader,
                    writer,
                    read_size=read_size,
                    measurement=measurement,
                )
        finally:
            _LOGGER.debug("stream_writer_close_start")
            await _close_stream_writer(writer)
    except asyncio.CancelledError:
        outcome = StreamOperationOutcome.CANCELLED
        raise
    except BaseException:
        outcome = StreamOperationOutcome.FAILED
        raise
    finally:
        _complete_stream_operation(measurement, outcome)


async def _relay_chunks(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
    *,
    read_size: int = _READ_SIZE,
    measurement: _StreamOperationMeasurement | None = None,
) -> StreamOperationOutcome:
    """Copy chunks downstream; drain to EOF with no writer, bounded after a close.

    A bounded drain that reaches EOF is the ordinary early-close outcome. One
    that times out leaves the upstream reader attached but unread, so the reader
    is released before returning: the producer then meets a broken pipe instead
    of a pipe buffer that never drains, and pipeline completion stays bounded.

    Returns
    -------
    StreamOperationOutcome
        ``EOF`` without a writer, ``DOWNSTREAM_CLOSED`` when the post-close
        drain finished, or ``POST_CLOSE_DRAIN_TIMEOUT`` when it timed out and
        the reader was released.
    """
    if writer is None:
        await _drain_stream_reader(
            reader,
            _DrainProgress(measurement=measurement),
            read_size=read_size,
        )
        return StreamOperationOutcome.EOF
    while True:
        chunk = await reader.read(read_size)
        _record_stream_read(measurement, chunk)
        if not chunk:
            return StreamOperationOutcome.EOF
        if await _write_to_stream_writer(writer, chunk) is _WriteOutcome.CLOSED:
            break
    discarded_bytes, timed_out = await _drain_stream_reader_bounded_result(
        reader,
        read_size=read_size,
        measurement=measurement,
    )
    _LOGGER.debug(
        "stream_downstream_closed discarded_bytes=%s",
        discarded_bytes,
        extra={"cuprum_discarded_bytes": discarded_bytes},
    )
    if timed_out:
        _release_stream_reader_transport(reader)
        return StreamOperationOutcome.POST_CLOSE_DRAIN_TIMEOUT
    return StreamOperationOutcome.DOWNSTREAM_CLOSED


async def _drain_stream_reader(
    reader: asyncio.StreamReader,
    progress: _DrainProgress,
    *,
    read_size: int = _READ_SIZE,
) -> int:
    """Consume the reader to EOF, discarding the data and returning byte count."""
    while True:
        chunk = await reader.read(read_size)
        _record_stream_read(progress.measurement, chunk)
        if not chunk:
            return progress.discarded_bytes
        progress.discarded_bytes += len(chunk)


async def _drain_stream_reader_bounded(
    reader: asyncio.StreamReader,
    *,
    read_size: int = _READ_SIZE,
) -> int:
    """Best-effort drain after downstream closure without waiting forever."""
    discarded_bytes, _ = await _drain_stream_reader_bounded_result(
        reader,
        read_size=read_size,
    )
    return discarded_bytes


async def _drain_stream_reader_bounded_result(
    reader: asyncio.StreamReader,
    *,
    read_size: int = _READ_SIZE,
    measurement: _StreamOperationMeasurement | None = None,
) -> tuple[int, bool]:
    """Drain after closure and report its partial byte count and timeout state."""
    progress = _DrainProgress(measurement=measurement)
    try:
        discarded_bytes = await asyncio.wait_for(
            _drain_stream_reader(reader, progress, read_size=read_size),
            timeout=_POST_CLOSE_DRAIN_TIMEOUT_S,
        )
    except TimeoutError:
        _LOGGER.debug(
            "stream_reader_drain_timeout timeout_s=%s",
            _POST_CLOSE_DRAIN_TIMEOUT_S,
            extra={"cuprum_timeout_s": _POST_CLOSE_DRAIN_TIMEOUT_S},
        )
        return progress.discarded_bytes, True
    return discarded_bytes, False


def _release_stream_reader_transport(reader: asyncio.StreamReader) -> None:
    """Close an upstream reader whose bounded post-close drain timed out.

    The drain's timeout means the pump is finished with this reader, but the
    reader is still attached to an open pipe. Left open, a producer that keeps
    writing fills the pipe buffer and blocks with no timeout, and pipeline
    completion waits for that stage forever. Closing the transport is what the
    loop would have done at EOF, and it makes the producer's next write fail
    with a broken pipe so the stage exits and the pipeline settles. That exit
    is an ordinary pipeline failure, so the fail-fast rule may then terminate
    other still-running stages, including a downstream stage that had stopped
    reading but had not yet finished.

    Nothing is released unless the transport is present and still open, so a
    reader without a transport — a test double, or an already-closed pipe —
    completes without error and repeated calls are safe. The reader itself is
    not reused afterwards; closing it discards any bytes still unread.

    Parameters
    ----------
    reader : asyncio.StreamReader
        The upstream reader the pump has finished draining.
    """
    transport = _stream_transport(reader)
    close = getattr(transport, "close", None)
    if not callable(close):
        return
    is_closing = getattr(transport, "is_closing", None)
    if callable(is_closing) and is_closing():
        return
    try:
        close()
    except (RuntimeError, OSError) as exc:
        _log_suppressed_stream_close_error(_READER_RELEASE_EVENT, "close", exc)


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
        _log_suppressed_stream_close_error(_WRITER_CLOSE_EVENT, "write_eof", exc)
    try:
        writer.close()
    except (BrokenPipeError, ConnectionResetError) as exc:
        _log_suppressed_stream_close_error(_WRITER_CLOSE_EVENT, "close", exc)
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
        _log_suppressed_stream_close_error(_WRITER_CLOSE_EVENT, "wait_closed", exc)


def _log_suppressed_stream_close_error(
    event: str,
    operation: str,
    exc: BaseException,
) -> None:
    """Log a cleanup error that cannot safely be raised during pipe teardown."""
    _LOGGER.debug(
        "%s operation=%s error=%s",
        event,
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
    "_release_stream_reader_transport",
    "_write_to_stream_writer",
]
