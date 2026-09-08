"""A queued asyncio pipe close must prevent native descriptor borrowing."""

from __future__ import annotations

import asyncio
import os
import typing as typ

import pytest

from cuprum._pipeline_stream_fds import _pause_reader_transport, _ReaderPause
from cuprum.pump_events import RustPumpDeclineReason

if typ.TYPE_CHECKING:
    import collections.abc as cabc


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
            assert await reader.read() == b"buffered prefix", (
                "the Python fallback must preserve bytes already read by asyncio"
            )
        finally:
            transport.close()

    asyncio.run(exercise())
