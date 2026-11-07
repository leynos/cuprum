"""Ordering of descriptor acquisition, draining, and native submission.

The native hand-off acquires its worker descriptors before the first await so a
descriptor number cannot be reused underneath it, but blocking activation and
executor submission stay after the buffered drain. These tests pin that split.
"""

from __future__ import annotations

import asyncio
import os
import typing as typ

import pytest

from cuprum import (
    _pipeline_stream_fds,
    _pipeline_stream_native_cleanup,
    _pipeline_streams,
)
from cuprum.unittests._pump_stream_dispatch_support import (
    _run_with_inline_executor,
    clear_backend_caches,
)
from cuprum.unittests._rust_pump_test_helpers import owned_fds

__all__ = ["clear_backend_caches"]

pytestmark = pytest.mark.usefixtures("clear_backend_caches")


class _OrderingRecorder:
    """Record each hand-off step and install the doubles that observe it."""

    def __init__(self) -> None:
        """Start with an empty call order and the real acquisition seam."""
        self.call_order: list[str] = []
        self._open_worker_fds = (
            _pipeline_stream_native_cleanup._open_native_pump_worker_fds
        )

    def pause_reader(
        self,
        reader: asyncio.StreamReader,
    ) -> _pipeline_stream_fds._ReaderPause:
        """Record a pause and return a resume callback for the reader."""
        del reader
        self.call_order.append("pause")
        return _pipeline_stream_fds._ReaderPause(resume=self._resume)

    def _resume(self) -> None:
        """Record that the reader transport was resumed."""
        self.call_order.append("resume")

    def acquire(
        self,
        **kwargs: int,
    ) -> _pipeline_stream_fds._NativePumpWorkerFds | None:
        """Record the synchronous acquisition before delegating."""
        self.call_order.append("acquire")
        return self._open_worker_fds(**kwargs)

    async def drain(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        """Record that the reader buffer was drained."""
        del reader, writer
        await asyncio.sleep(0)
        self.call_order.append("drain")

    def engage(self, **_kwargs: object) -> tuple[bool, bool]:
        """Record blocking-mode activation of the acquired pair."""
        self.call_order.append("engage")
        return True, True

    def restore(self, **_: object) -> None:
        """Record restoration of the transport's original blocking mode."""
        self.call_order.append("restore")

    def submit(self, reader_fd: int, writer_fd: int) -> int:
        """Consume the duplicate writer FD exactly as the Rust pump does."""
        del reader_fd
        self.call_order.append("submit")
        os.close(writer_fd)
        return 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Install every double that records one step of the hand-off."""
        import cuprum._streams_rs as streams_rs

        monkeypatch.setattr(
            _pipeline_streams,
            "_pause_reader_transport",
            self.pause_reader,
        )
        monkeypatch.setattr(
            _pipeline_stream_native_cleanup,
            "_open_native_pump_worker_fds",
            self.acquire,
        )
        monkeypatch.setattr(_pipeline_streams, "_drain_reader_buffer", self.drain)
        monkeypatch.setattr(
            _pipeline_stream_fds,
            "_set_stream_fds_blocking",
            self.engage,
        )
        monkeypatch.setattr(
            _pipeline_stream_fds,
            "_restore_stream_fd_blocking",
            self.restore,
        )
        monkeypatch.setattr(streams_rs, "rust_pump_stream", self.submit)


def _run_pump(reader_fd: int, writer_fd: int) -> None:
    """Run the native pump against a private pipe's descriptors."""
    asyncio.run(
        _run_with_inline_executor(
            _pipeline_streams._run_rust_pump(
                reader=typ.cast("asyncio.StreamReader", object()),
                writer=None,
                handoff=_pipeline_streams._RustPumpHandoff(
                    reader_fd=reader_fd,
                    writer_fd=writer_fd,
                    cleanup_grace_s=0.5,
                ),
            )
        )
    )


class TestAcquisitionOrder:
    """Ordering tests for the native pump hand-off."""

    def test_run_rust_pump_acquires_before_draining(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rust pumping should acquire descriptors before the first await."""
        recorder = _OrderingRecorder()
        recorder.install(monkeypatch)

        with owned_fds() as (reader_fd, writer_fd):
            _run_pump(reader_fd, writer_fd)

        assert recorder.call_order == [
            "pause",
            "acquire",
            "drain",
            "engage",
            "submit",
            "restore",
            "resume",
        ], (
            "acquisition must precede the first await, while blocking activation "
            "and submission stay after the buffered drain"
        )

    def test_suspended_drain_does_not_submit_native_work(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A drain held open must not have submitted native work yet."""
        drain_started = asyncio.Event()
        release_drain = asyncio.Event()
        submitted: list[int] = []

        async def held_drain(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter | None,
        ) -> None:
            """Hold the drain open until the test releases it."""
            del reader, writer
            drain_started.set()
            await release_drain.wait()

        def record_submission(reader_fd: int, writer_fd: int) -> int:
            """Record a native submission that must wait for the drain."""
            del reader_fd
            submitted.append(writer_fd)
            os.close(writer_fd)
            return 0

        monkeypatch.setattr(_pipeline_streams, "_drain_reader_buffer", held_drain)

        import cuprum._streams_rs as streams_rs

        monkeypatch.setattr(streams_rs, "rust_pump_stream", record_submission)

        async def exercise(reader_fd: int, writer_fd: int) -> None:
            """Start the pump, observe the held drain, then release it."""
            task = asyncio.create_task(
                _run_with_inline_executor(
                    _pipeline_streams._run_rust_pump(
                        reader=typ.cast("asyncio.StreamReader", object()),
                        writer=None,
                        handoff=_pipeline_streams._RustPumpHandoff(
                            reader_fd=reader_fd,
                            writer_fd=writer_fd,
                            cleanup_grace_s=0.5,
                        ),
                    )
                )
            )
            await asyncio.wait_for(drain_started.wait(), timeout=1.0)
            assert not submitted, (
                "native work must not be submitted while the drain is suspended"
            )
            release_drain.set()
            await task

        with owned_fds() as (reader_fd, writer_fd):
            asyncio.run(exercise(reader_fd, writer_fd))

        assert len(submitted) == 1, "the released drain must submit native work once"
