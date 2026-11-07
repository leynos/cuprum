"""Acquisition and drain failures around the native pump hand-off.

The hand-off acquires its worker descriptors before the buffered drain. These
tests pin the two failure paths that ordering creates: acquisition can fail
before anything is drained, and the drain can fail or be cancelled after the
pair is already owned. Both must leave the hop safe for the Python fallback.
"""

from __future__ import annotations

import asyncio
import os
import typing as typ
from unittest import mock

import pytest

from cuprum import (
    ScopeConfig,
    _pipeline_stream_fds,
    _pipeline_stream_native_cleanup,
    _pipeline_streams,
    scoped,
    sh,
)
from cuprum._testing import set_rust_availability_for_testing
from cuprum.unittests._pump_stream_dispatch_support import (
    _FdTransport,
    _nonblocking_pipe_pair,
    _RecordingWriter,
    clear_backend_caches,
)
from cuprum.unittests._rust_pump_test_helpers import owned_fds
from tests.helpers.catalogue import python_catalogue

__all__ = ["clear_backend_caches"]

pytestmark = pytest.mark.usefixtures("clear_backend_caches")

_PREFIX = b"buffered prefix"


def test_acquisition_failure_skips_drain_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed acquisition resumes the reader before the Python pump runs."""
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
    set_rust_availability_for_testing(is_available=True)
    drain_calls = 0

    async def record_drain(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        """Record an unexpected buffered drain."""
        del reader, writer
        nonlocal drain_calls
        drain_calls += 1
        await asyncio.sleep(0)

    monkeypatch.setattr(_pipeline_streams, "_drain_reader_buffer", record_drain)
    monkeypatch.setattr(
        _pipeline_stream_native_cleanup,
        "_open_native_pump_worker_fds",
        lambda **_kwargs: None,
    )

    async def exercise() -> tuple[int, int, int, bytes, bool]:
        """Drive dispatch with a reader whose acquisition must fail."""
        reader = asyncio.StreamReader()
        reader_transport = _FdTransport(read_fd)
        reader.set_transport(typ.cast("asyncio.BaseTransport", reader_transport))
        reader.feed_data(_PREFIX)
        reader.feed_eof()
        writer = _RecordingWriter(write_fd)
        await _pipeline_streams._pump_stream_dispatch(
            reader,
            typ.cast("asyncio.StreamWriter", writer),
        )
        return (
            reader_transport.pause_calls,
            reader_transport.resume_calls,
            drain_calls,
            bytes(writer.written),
            writer.eof,
        )

    with _nonblocking_pipe_pair() as (
        read_fd,
        read_write_fd,
        write_read_fd,
        write_fd,
    ):
        del read_write_fd, write_read_fd
        pauses, resumes, drains, delivered, eof = asyncio.run(exercise())

    assert pauses == 1, "the reader must be paused"
    assert resumes == 1, (
        "a failed acquisition must resume the reader before the fallback"
    )
    assert drains == 0, "a failed acquisition must not drain the buffer"
    assert delivered == _PREFIX, (
        "the Python pump must deliver the still-buffered prefix"
    )
    assert eof, "the Python pump must signal downstream EOF"


class _DrainFailureHarness:
    """Drive a drain failure over an acquired pair and record its release."""

    def __init__(self, error: BaseException) -> None:
        """Capture the failure and the seams that observe cleanup."""
        self.error = error
        self.resume_calls = 0
        self.acquired: list[int] = []
        self.close_worker_fd = mock.Mock(
            wraps=_pipeline_stream_native_cleanup._close_native_pump_worker_fd
        )
        self._original_open = (
            _pipeline_stream_native_cleanup._open_native_pump_worker_fds
        )

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Install the pause, drain-failure, and recording seams."""
        monkeypatch.setattr(
            _pipeline_streams,
            "_pause_reader_transport",
            self.pause_reader,
        )
        monkeypatch.setattr(_pipeline_streams, "_drain_reader_buffer", self.fail_drain)
        monkeypatch.setattr(
            _pipeline_stream_native_cleanup,
            "_open_native_pump_worker_fds",
            self.record_open,
        )
        monkeypatch.setattr(
            _pipeline_stream_native_cleanup,
            "_close_native_pump_worker_fd",
            self.close_worker_fd,
        )

    def pause_reader(
        self,
        reader: asyncio.StreamReader,
    ) -> _pipeline_stream_fds._ReaderPause:
        """Return a pause whose resume records restoration of the reader."""
        del reader
        return _pipeline_stream_fds._ReaderPause(resume=self.resume)

    def resume(self) -> None:
        """Record restoration of the paused reader."""
        self.resume_calls += 1

    def record_open(
        self,
        **kwargs: int,
    ) -> _pipeline_stream_fds._NativePumpWorkerFds | None:
        """Record the worker pair the acquisition actually opened."""
        worker_fds = self._original_open(**kwargs)
        if worker_fds is not None:
            self.acquired.extend([worker_fds.reader_fd, worker_fds.writer_fd])
        return worker_fds

    async def fail_drain(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        """Raise the parametrized drain failure."""
        del reader, writer
        await asyncio.sleep(0)
        raise self.error

    def run(self) -> None:
        """Run the hand-off over a private pipe and assert its rollback."""
        with owned_fds() as (reader_fd, writer_fd):
            handoff = _pipeline_stream_native_cleanup._RustPumpHandoff(
                reader_fd=reader_fd,
                writer_fd=writer_fd,
                cleanup_grace_s=0.5,
            )
            with pytest.raises(type(self.error)) as exc_info:
                asyncio.run(
                    _pipeline_streams._run_rust_pump(
                        reader=typ.cast("asyncio.StreamReader", object()),
                        writer=None,
                        handoff=handoff,
                    )
                )
            if isinstance(self.error, OSError):
                assert exc_info.value is self.error, (
                    "the drain failure must propagate unchanged"
                )
            self._assert_released(reader_fd, writer_fd)
        assert self.resume_calls == 1, "the failure must resume the paused reader once"

    def _assert_released(self, reader_fd: int, writer_fd: int) -> None:
        """Assert the unactivated pair closed once and the transports survived."""
        assert len(self.acquired) == 2, "acquisition must open both worker descriptors"
        assert self.close_worker_fd.call_args_list == [
            mock.call(self.acquired[0]),
            mock.call(self.acquired[1]),
        ], "the unactivated pair must be closed exactly once in reader-first order"
        for fd in self.acquired:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(fd)
        os.fstat(reader_fd)
        os.fstat(writer_fd)


@pytest.mark.parametrize(
    "error",
    [OSError("drain failed"), asyncio.CancelledError()],
    ids=["oserror", "cancelled"],
)
def test_drain_failure_closes_acquired_pair_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    """A drain failure releases the acquired pair and re-raises unchanged."""
    harness = _DrainFailureHarness(error)
    harness.install(monkeypatch)

    harness.run()


def test_acquisition_failure_keeps_public_pipeline_output_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed acquisition falls back without disturbing the public pipeline.

    The hop is forced onto the native backend and its acquisition seam is made
    to decline, so the end-to-end run proves the Python fallback carries the
    payload and both stages complete rather than that the private seam merely
    returns ``False``.
    """
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
    set_rust_availability_for_testing(is_available=True)
    monkeypatch.setattr(
        _pipeline_stream_native_cleanup,
        "_open_native_pump_worker_fds",
        lambda **_kwargs: None,
    )
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    producer = python("-c", "import sys; sys.stdout.write('buffered prefix')")
    consumer = python(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read().upper())",
    )

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = (producer | consumer).run_sync(timeout=5.0)

    assert result.stdout == "BUFFERED PREFIX", (
        "the Python fallback must deliver the pipeline payload unchanged"
    )
    assert [stage.exit_code for stage in result.stages] == [0, 0], (
        "both stages must complete despite the failed acquisition"
    )
