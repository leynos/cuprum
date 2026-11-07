"""A queued asyncio pipe close must prevent native descriptor borrowing."""

from __future__ import annotations

import asyncio
import os
import typing as typ

import pytest

from cuprum import _pipeline_stream_native_cleanup, _pipeline_streams
from cuprum._pipeline_stream_fds import _pause_reader_transport, _ReaderPause
from cuprum._testing import set_rust_availability_for_testing
from cuprum.pump_events import PumpEvent, RustPumpDeclineReason
from cuprum.pump_observation import observe_pump
from cuprum.unittests._pump_stream_dispatch_support import (
    _FdTransport,
    _nonblocking_pipe_pair,
    _RecordingWriter,
    clear_backend_caches,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# The fixture is requested by name in ``usefixtures``, so the import is the
# only thing that makes it visible to pytest.
__all__ = ["clear_backend_caches"]

_PREFIX = b"buffered prefix"


@pytest.fixture
def pipe_at_eof() -> cabc.Iterator[typ.BinaryIO]:
    """Yield a real read pipe containing a prefix followed by kernel EOF."""
    reader_fd, writer_fd = os.pipe()
    with (
        os.fdopen(reader_fd, "rb", buffering=0) as reader,
        os.fdopen(writer_fd, "wb", buffering=0) as writer,
    ):
        writer.write(b"buffered prefix")
        writer.close()
        yield reader


class _ClosingReaderProtocol(asyncio.StreamReaderProtocol):
    """Observe hand-off during real EOF notification, before queued close."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        outcome: asyncio.Future[_ReaderPause],
    ) -> None:
        """Retain the reader and a completion signal for the EOF observation."""
        super().__init__(reader)
        self.reader = reader
        self.outcome = outcome

    def eof_received(self) -> bool | None:
        """Attempt the real pause while the transport is already closing."""
        self.outcome.set_result(_pause_reader_transport(self.reader))
        return super().eof_received()


@pytest.mark.skipif(os.name == "nt", reason="Unix read-pipe EOF callback contract")
def test_closing_reader_declines_native_borrow(pipe_at_eof: typ.BinaryIO) -> None:
    """Queued close invalidates the FD, but Python retains the buffered prefix."""

    async def exercise() -> None:
        """Observe real EOF and complete the Python fallback without native I/O."""
        reader = asyncio.StreamReader()
        outcome: asyncio.Future[_ReaderPause] = (
            asyncio.get_running_loop().create_future()
        )
        protocol = _ClosingReaderProtocol(reader, outcome)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: protocol, pipe_at_eof
        )
        try:
            pause = await outcome
            assert transport.is_closing(), "the actual transport must be closing"
            assert pipe_at_eof.closed, "queued close invalidates the retained FD"
            assert not pause.may_hand_off, "a closing descriptor cannot be borrowed"
            assert pause.decline_reason is RustPumpDeclineReason.READER_PAUSE_FAILED, (
                "an ignored pause must use the existing pause-failure decline"
            )
            assert pause.closing_transport, (
                "the pause must record the transport close that denied hand-off"
            )
            assert await reader.read() == b"buffered prefix", (
                "the Python fallback must preserve bytes already read by asyncio"
            )
        finally:
            transport.close()

    asyncio.run(exercise())


class _ClosingFdTransport(_FdTransport):
    """Transport double that reports itself as already closing."""

    def is_closing(self) -> bool:
        """Report the closing state that invalidates descriptor borrowing."""
        return True


@pytest.mark.usefixtures("clear_backend_caches")
def test_closing_reader_dispatch_falls_back_without_acquiring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closing transport declines before acquisition and Python pumps the hop."""
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
    set_rust_availability_for_testing(is_available=True)
    events: list[PumpEvent] = []
    acquisition_calls = 0
    native_start_calls = 0

    def record_acquisition(**_kwargs: int) -> None:
        """Record an acquisition that must never run for a closing reader."""
        nonlocal acquisition_calls
        acquisition_calls += 1

    def record_native_start(*_args: object, **_kwargs: object) -> None:
        """Record an attempt to submit native work that must never run."""
        nonlocal native_start_calls
        native_start_calls += 1

    monkeypatch.setattr(
        _pipeline_stream_native_cleanup,
        "_open_native_pump_worker_fds",
        record_acquisition,
    )
    monkeypatch.setattr(
        _pipeline_stream_native_cleanup,
        "_start_rust_pump_with_cleanup",
        record_native_start,
    )

    async def exercise() -> tuple[_FdTransport, bytes, bool, bool]:
        """Drive dispatch with a reader past EOF whose transport is closing."""
        reader = asyncio.StreamReader()
        reader_transport = _ClosingFdTransport(read_fd)
        reader.set_transport(typ.cast("asyncio.BaseTransport", reader_transport))
        reader.feed_data(_PREFIX)
        reader.feed_eof()
        writer = _RecordingWriter(write_fd)
        await _pipeline_streams._pump_stream_dispatch(
            reader,
            typ.cast("asyncio.StreamWriter", writer),
        )
        return reader_transport, bytes(writer.written), writer.eof, writer.closed

    with _nonblocking_pipe_pair() as (
        read_fd,
        read_write_fd,
        write_read_fd,
        write_fd,
    ):
        del read_write_fd, write_read_fd
        with observe_pump(events.append):
            transport, delivered, eof, closed = asyncio.run(exercise())

    assert [event.phase for event in events] == ["declined"], (
        f"the closing reader must only report a decline, found {events!r}"
    )
    assert events[0].reason == RustPumpDeclineReason.READER_PAUSE_FAILED, (
        "the decline must name the ignored pause on the closing transport"
    )
    assert acquisition_calls == 0, (
        "a closing reader must decline before opening worker descriptors"
    )
    assert native_start_calls == 0, "no native worker may be submitted"
    assert transport.pause_calls == 0, (
        "asyncio would ignore a pause on a closing transport"
    )
    assert transport.resume_calls == 0, "nothing was paused, so nothing resumes"
    assert delivered == _PREFIX, "the Python pump must deliver the prefix in order"
    assert eof, "the Python pump must signal downstream EOF"
    assert closed, "the Python pump must close the writer"
