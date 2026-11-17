"""Stream endpoint doubles shared by the inter-stage pump tests.

The doubles exist so pump behaviour can be driven without subprocesses: a
reader that queues chunks then reaches EOF, a reader that stalls after one
chunk so the bounded post-close drain times out, and a writer that records
writes, drains, and closure while failing its drain on a chosen call.

Their use is confined to tests that exercise ``cuprum._streams_pump`` and the
reader-release contract beside it. Production code must not import them, and a
test that needs an endpoint with different behaviour should add a double here
rather than grow a local variant of one of these.
"""

from __future__ import annotations

import asyncio

__all__ = [
    "_RecordingTransport",
    "_StallingPumpReader",
    "_StubPumpReader",
    "_StubPumpWriter",
]


class _RecordingTransport:
    """Transport double recording close requests and its closing state."""

    def __init__(
        self,
        *,
        is_closing: bool = False,
        error: Exception | None = None,
    ) -> None:
        """Initialize the double with a closing state and an optional failure."""
        self.close_calls = 0
        self._is_closing = is_closing
        self._error = error

    def is_closing(self) -> bool:
        """Report whether the transport has already started closing."""
        return self._is_closing

    def close(self) -> None:
        """Record the close, optionally raising as a real transport might."""
        self.close_calls += 1
        self._is_closing = True
        if self._error is not None:
            raise self._error


class _StubPumpReader:
    """Stub stream reader yielding queued chunks then EOF."""

    def __init__(
        self,
        chunks: list[bytes],
        *,
        delay_s: float = 0.0,
        transport: object | None = None,
    ) -> None:
        """Initialize the stub with queued chunks, a delay, and a transport."""
        self._chunks = list(chunks)
        self._delay_s = delay_s
        self.transport = transport
        self.read_calls = 0
        self.read_sizes: list[int] = []

    async def read(self, size: int) -> bytes:
        """Return the next queued chunk after a delay, or empty bytes at EOF."""
        self.read_calls += 1
        self.read_sizes.append(size)
        await asyncio.sleep(self._delay_s)
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _StallingPumpReader:
    """Stub stream reader that emits one configured chunk then stalls."""

    def __init__(
        self,
        first_chunk: bytes,
        *,
        transport: object | None = None,
    ) -> None:
        """Initialize the reader with its chunk and an optional transport."""
        self._first_chunk = first_chunk
        self.transport = transport
        self.read_calls = 0

    async def read(self, _: int) -> bytes:
        """Return one chunk, then wait forever unless the pump cancels the read."""
        self.read_calls += 1
        if self.read_calls == 1:
            return self._first_chunk
        await asyncio.sleep(3600)
        return b"more"


class _StubPumpWriter:
    """Stub stream writer recording writes, drains, and closure."""

    def __init__(self, *, fail_on_drain_call: int | None = None) -> None:
        """Initialize the stub with an optional drain failure point."""
        self.data = bytearray()
        self.drain_calls = 0
        self.write_calls = 0
        self.closed = False
        self.write_eof_calls = 0
        self.wait_closed_calls = 0
        self._fail_on_drain_call = fail_on_drain_call

    def write(self, chunk: bytes) -> None:
        """Buffer the chunk and record the write call."""
        self.write_calls += 1
        self.data.extend(chunk)

    async def drain(self) -> None:
        """Record drain and optionally simulate downstream closure."""
        self.drain_calls += 1
        await asyncio.sleep(0)
        if self.drain_calls == self._fail_on_drain_call:
            raise BrokenPipeError

    def write_eof(self) -> None:
        """Record that end-of-file was signalled to the writer."""
        self.write_eof_calls += 1

    def close(self) -> None:
        """Mark the writer as closed."""
        self.closed = True

    async def wait_closed(self) -> None:
        """Record that closure was awaited."""
        self.wait_closed_calls += 1
