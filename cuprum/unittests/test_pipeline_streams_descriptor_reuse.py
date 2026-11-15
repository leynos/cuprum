"""Descriptor-number reuse must not redirect a native pump worker.

The native hand-off carries descriptor *numbers*. If the worker descriptors
were opened after the buffered drain, a transport that closed during that
await could release its number for another pipeline to reuse, and the worker
would attach to the wrong pipe. These tests pin the acquisition-before-await
ordering and the fallback paths that keep the hop safe when acquisition fails.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import os
import select
import typing as typ
from unittest import mock

import pytest

from cuprum import _pipeline_streams
from cuprum._testing import set_rust_availability_for_testing
from cuprum.unittests._pump_stream_dispatch_support import (
    _RecordingWriter,
    _run_with_inline_executor,
    clear_backend_caches,
)

__all__ = ["clear_backend_caches"]

pytestmark = pytest.mark.usefixtures("clear_backend_caches")

_PREFIX = b"buffered prefix"
_SENTINEL = b"replacement sentinel"


class _HeldDrainWriter(_RecordingWriter):
    """Writer double whose drain suspends until the test releases it."""

    def __init__(
        self,
        fd: int,
        drain_started: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        """Retain the barrier events that hold the buffered drain open."""
        super().__init__(fd)
        self._drain_started = drain_started
        self._release = release

    async def drain(self) -> None:
        """Signal that the drain is suspended, then wait for the test."""
        self._drain_started.set()
        await self._release.wait()


@dc.dataclass(frozen=True, slots=True)
class _ReuseObservation:
    """Values recorded while the descriptor number was reused."""

    worker_reader_device: int
    worker_reader_inode: int
    original_reader_device: int
    original_reader_inode: int
    replacement_reader_device: int
    replacement_reader_inode: int
    replacement_blocking: bool
    replacement_blocking_after: bool
    replacement_still_open: bool
    replacement_sentinel: bytes
    worker_leaked: bytes
    delivered: bytes
    writer_closed: bool


class _ReuseSpy:
    """Native-pump double that records the pipe its reader attached to."""

    def __init__(self) -> None:
        """Start with no recorded worker descriptor."""
        self.worker_stat: os.stat_result | None = None
        self.worker_leaked = b""

    def __call__(self, reader_fd: int, writer_fd: int) -> int:
        """Record the worker's pipe and prove it holds no replacement bytes."""
        self.worker_stat = os.fstat(reader_fd)
        was_blocking = os.get_blocking(reader_fd)
        try:
            os.set_blocking(reader_fd, False)
            try:
                self.worker_leaked = os.read(reader_fd, len(_SENTINEL))
            except BlockingIOError:
                self.worker_leaked = b""
        finally:
            os.set_blocking(reader_fd, was_blocking)
        # The native pump consumes its writer duplicate.
        os.close(writer_fd)
        return 0


@dc.dataclass(frozen=True, slots=True)
class _ReplacementState:
    """State of the descriptor number after it was reassigned."""

    still_open: bool
    blocking: bool
    sentinel: bytes


def _close_quietly(*fds: int) -> None:
    """Close descriptors that a test path may already have released."""
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)


async def _open_reader_transport(
    read_fd: int,
) -> tuple[asyncio.StreamReader, asyncio.ReadTransport, typ.IO[bytes]]:
    """Connect an asyncio read transport to a real pipe descriptor."""
    reader_file = os.fdopen(read_fd, "rb", buffering=0)
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.connect_read_pipe(lambda: protocol, reader_file)
    return reader, transport, reader_file


def _observe_replacement(read_fd: int) -> _ReplacementState:
    """Record the reused descriptor number's state after the hand-off."""
    still_open = True
    try:
        os.fstat(read_fd)
    except OSError:
        still_open = False
    blocking = os.get_blocking(read_fd)
    readable, _, _ = select.select([read_fd], [], [], 0)
    sentinel = os.read(read_fd, len(_SENTINEL)) if readable else b""
    return _ReplacementState(
        still_open=still_open, blocking=blocking, sentinel=sentinel
    )


@dc.dataclass(slots=True)
class _ReuseHarness:
    """Real pipes and transport used to force descriptor-number reuse."""

    original_read_fd: int
    original_write_fd: int
    writer_read_fd: int
    writer_fd: int
    reader: asyncio.StreamReader
    transport: asyncio.ReadTransport
    reader_file: typ.IO[bytes]
    drain_started: asyncio.Event
    release: asyncio.Event
    writer: _HeldDrainWriter
    spy: _ReuseSpy
    replacement_write_fd: int = -1

    @classmethod
    async def create(cls) -> _ReuseHarness:
        """Open the pipes and connect the reader transport under test."""
        original_read_fd, original_write_fd = os.pipe()
        writer_read_fd, writer_fd = os.pipe()
        reader, transport, reader_file = await _open_reader_transport(original_read_fd)
        reader.feed_data(_PREFIX)
        reader.feed_eof()
        drain_started = asyncio.Event()
        release = asyncio.Event()
        return cls(
            original_read_fd=original_read_fd,
            original_write_fd=original_write_fd,
            writer_read_fd=writer_read_fd,
            writer_fd=writer_fd,
            reader=reader,
            transport=transport,
            reader_file=reader_file,
            drain_started=drain_started,
            release=release,
            writer=_HeldDrainWriter(writer_fd, drain_started, release),
            spy=_ReuseSpy(),
        )

    def close(self) -> None:
        """Release every descriptor the harness opened."""
        _close_quietly(
            self.original_read_fd,
            self.original_write_fd,
            self.writer_read_fd,
            self.writer_fd,
            self.replacement_write_fd,
        )


async def _exercise_descriptor_reuse() -> _ReuseObservation:
    """Reuse the reader's descriptor number while the buffered drain is held."""
    harness = await _ReuseHarness.create()
    try:
        import cuprum._streams_rs as streams_rs

        with mock.patch.object(streams_rs, "rust_pump_stream", harness.spy):
            task = asyncio.create_task(
                _run_with_inline_executor(
                    _pipeline_streams._pump_stream_dispatch(
                        harness.reader,
                        typ.cast("asyncio.StreamWriter", harness.writer),
                    )
                )
            )
            await asyncio.wait_for(harness.drain_started.wait(), timeout=1.0)
            original_stat = os.fstat(harness.original_read_fd)
            # Create the replacement while the original number is still held,
            # so ``os.pipe`` cannot hand back the number being reused.
            replacement_read_fd, harness.replacement_write_fd = os.pipe()
            harness.transport.close()
            # Let the queued connection_lost callback close the descriptor.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert harness.reader_file.closed, (
                "the transport must release its descriptor"
            )
            os.dup2(replacement_read_fd, harness.original_read_fd)
            os.close(replacement_read_fd)
            replacement_stat = os.fstat(harness.original_read_fd)
            replacement_blocking = os.get_blocking(harness.original_read_fd)
            os.write(harness.replacement_write_fd, _SENTINEL)
            harness.release.set()
            await task

        assert harness.spy.worker_stat is not None, "the native worker must have run"
        replacement_state = _observe_replacement(harness.original_read_fd)
        return _ReuseObservation(
            worker_reader_device=harness.spy.worker_stat.st_dev,
            worker_reader_inode=harness.spy.worker_stat.st_ino,
            original_reader_device=original_stat.st_dev,
            original_reader_inode=original_stat.st_ino,
            replacement_reader_device=replacement_stat.st_dev,
            replacement_reader_inode=replacement_stat.st_ino,
            replacement_blocking=replacement_blocking,
            replacement_blocking_after=replacement_state.blocking,
            replacement_still_open=replacement_state.still_open,
            replacement_sentinel=replacement_state.sentinel,
            worker_leaked=harness.spy.worker_leaked,
            delivered=bytes(harness.writer.written),
            writer_closed=harness.writer.closed,
        )
    finally:
        harness.close()


@pytest.mark.skipif(os.name == "nt", reason="Unix descriptor-number reuse")
def test_worker_attaches_to_original_pipe_when_descriptor_number_is_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reused descriptor number must not redirect the native worker."""
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
    set_rust_availability_for_testing(is_available=True)

    observation = asyncio.run(_exercise_descriptor_reuse())

    assert (
        observation.worker_reader_device,
        observation.worker_reader_inode,
    ) == (
        observation.original_reader_device,
        observation.original_reader_inode,
    ), "the worker must attach to the pipe the hand-off was authorized to borrow"
    assert (
        observation.worker_reader_device,
        observation.worker_reader_inode,
    ) != (
        observation.replacement_reader_device,
        observation.replacement_reader_inode,
    ), "the worker must not attach to the pipe that reused the descriptor number"
    assert observation.worker_leaked == b"", (
        "the worker must not read bytes written to the replacement pipe"
    )
    assert observation.replacement_sentinel == _SENTINEL, (
        "the replacement pipe's sentinel must remain unread"
    )
    assert observation.replacement_still_open, (
        "the replacement descriptor must remain open"
    )
    assert observation.replacement_blocking_after == observation.replacement_blocking, (
        "the replacement descriptor's blocking mode must be unchanged"
    )
    assert observation.delivered == _PREFIX, (
        "the buffered prefix must reach downstream exactly once"
    )
    assert observation.writer_closed, "the native path must close the writer"
