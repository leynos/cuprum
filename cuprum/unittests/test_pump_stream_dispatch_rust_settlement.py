"""Regression tests for native-pump executor settlement cleanup."""

from __future__ import annotations

import asyncio
import os
import typing as typ
from unittest import mock

import pytest

from cuprum import _pipeline_stream_fds, _pipeline_streams
from cuprum.unittests._pump_stream_dispatch_support import (
    _nonblocking_pipe_pair,
    _run_with_inline_executor_returning,
    clear_backend_caches,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ["clear_backend_caches"]

pytestmark = pytest.mark.usefixtures("clear_backend_caches")


class _HeldNativePump:
    """Executor double that keeps submitted native work pending."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        """Create a pending future controlled by the test."""
        self.future = loop.create_future()
        self.submitted = asyncio.Event()
        self.received_fds: list[int] = []

    def retain_writer(self, reader_fd: int, writer_fd: int) -> int:
        """Record the native duplicate without closing it."""
        del reader_fd
        self.received_fds.append(writer_fd)
        return 0

    def close_received_writer(self) -> None:
        """Model Rust consuming its duplicate before worker completion."""
        os.close(self.received_fds[0])

    def submit(
        self,
        executor: object,
        function: object,
        *args: object,
    ) -> asyncio.Future[object]:
        """Start native work and defer publication of its completion."""
        del executor
        typ.cast("cabc.Callable[..., object]", function)(*args)
        self.submitted.set()
        return self.future


class _CleanupOrder:
    """Record reader cleanup performed at native-worker settlement."""

    def __init__(self) -> None:
        """Capture the unpatched restoration function and event log."""
        self.finished = asyncio.Event()
        self.order: list[str] = []
        self._restore = _pipeline_stream_fds._restore_stream_fd_blocking

    def pause(self, reader: asyncio.StreamReader) -> _pipeline_stream_fds._ReaderPause:
        """Record pausing and return a completion-owned resume callback."""
        del reader
        self.order.append("pause")
        return _pipeline_stream_fds._ReaderPause(
            may_hand_off=True,
            resume=self.resume,
        )

    def resume(self) -> None:
        """Record reader resumption after descriptor restoration."""
        self.order.append("resume")
        self.finished.set()

    async def drain(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        """Record draining before yielding to executor submission."""
        del reader, writer
        self.order.append("drain")
        await asyncio.sleep(0)

    def restore(
        self,
        *,
        reader_fd: int,
        writer_fd: int,
        reader_was_blocking: bool,
        writer_was_blocking: bool,
    ) -> None:
        """Record and perform descriptor-mode restoration."""
        self.order.append("restore")
        self._restore(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            reader_was_blocking=reader_was_blocking,
            writer_was_blocking=writer_was_blocking,
        )


class TestRustPumpSettlement:
    """Regression tests for completion-owned native-pump cleanup."""

    def test_run_rust_pump_defers_cleanup_until_worker_settles(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancellation should defer restoration and resumption to completion."""
        asyncio.run(_cancel_before_native_worker_settles(monkeypatch))

    def test_run_rust_pump_suppresses_writer_close_oserror(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A successful native pump should suppress writer-close I/O failures."""
        close_writer = mock.AsyncMock(side_effect=OSError("writer closed"))

        def close_duplicate(reader_fd: int, writer_fd: int) -> int:
            """Model native ownership by closing the supplied duplicate."""
            del reader_fd
            os.close(writer_fd)
            return 0

        import cuprum._streams_rs as streams_rs

        monkeypatch.setattr(streams_rs, "rust_pump_stream", close_duplicate)
        monkeypatch.setattr(_pipeline_streams, "_close_stream_writer", close_writer)

        with _nonblocking_pipe_pair() as (
            read_fd,
            read_write_fd,
            write_read_fd,
            write_fd,
        ):
            del read_write_fd, write_read_fd
            result = asyncio.run(
                _run_with_inline_executor_returning(
                    _pipeline_streams._run_rust_pump(
                        reader=typ.cast("asyncio.StreamReader", object()),
                        writer=None,
                        reader_fd=read_fd,
                        writer_fd=write_fd,
                    )
                )
            )

        assert result is True, "expected successful native pumping to report handled"
        close_writer.assert_awaited_once_with(None)


async def _settle_cancelled_native_pump_and_verify_descriptor_ownership(
    *,
    native_pump: _HeldNativePump,
    cleanup: _CleanupOrder,
    close_duplicate: mock.Mock,
    write_fd: int,
    task: asyncio.Task[bool],
) -> None:
    """Settle native work and verify it never closes asyncio's writer."""
    native_pump.close_received_writer()
    os.fstat(write_fd)
    reused_writer_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        assert reused_writer_fd == native_pump.received_fds[0], (
            "the native duplicate's numeric slot should be available for reuse"
        )
        native_pump.future.set_result(0)
        await asyncio.wait_for(cleanup.finished.wait(), timeout=0.5)

        assert cleanup.order == ["pause", "drain", "restore", "resume"], (
            "expected settlement to restore descriptors before resuming reader"
        )
        os.fstat(write_fd)
        os.fstat(reused_writer_fd)
        close_duplicate.assert_not_called()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        os.close(reused_writer_fd)


async def _cancel_before_native_worker_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel a pump task and verify completion-owned cleanup ordering."""
    loop = asyncio.get_running_loop()
    native_pump = _HeldNativePump(loop)
    cleanup = _CleanupOrder()
    close_duplicate = mock.Mock(wraps=_pipeline_streams._close_rust_writer_fd)

    import cuprum._streams_rs as streams_rs

    monkeypatch.setattr(streams_rs, "rust_pump_stream", native_pump.retain_writer)
    monkeypatch.setattr(_pipeline_streams, "_pause_reader_transport", cleanup.pause)
    monkeypatch.setattr(_pipeline_streams, "_drain_reader_buffer", cleanup.drain)
    monkeypatch.setattr(
        _pipeline_streams,
        "_close_rust_writer_fd",
        close_duplicate,
    )
    monkeypatch.setattr(
        _pipeline_stream_fds, "_restore_stream_fd_blocking", cleanup.restore
    )

    with _nonblocking_pipe_pair() as (
        read_fd,
        read_write_fd,
        write_read_fd,
        write_fd,
    ):
        del read_write_fd, write_read_fd
        with mock.patch.object(
            loop,
            "run_in_executor",
            side_effect=native_pump.submit,
        ):
            task = asyncio.create_task(
                _pipeline_streams._run_rust_pump(
                    reader=typ.cast("asyncio.StreamReader", object()),
                    writer=None,
                    reader_fd=read_fd,
                    writer_fd=write_fd,
                )
            )
            await asyncio.wait_for(native_pump.submitted.wait(), timeout=0.5)
            task.cancel()
            await asyncio.sleep(0)

            assert cleanup.order == ["pause", "drain"], (
                "expected FD restoration and reader resumption to wait for native "
                "worker settlement"
            )
            assert not task.done(), (
                "expected cancellation to await the native worker's cleanup callback"
            )
            assert native_pump.received_fds, (
                "expected native work to receive a writer duplicate"
            )
            os.fstat(native_pump.received_fds[0])

            await _settle_cancelled_native_pump_and_verify_descriptor_ownership(
                native_pump=native_pump,
                cleanup=cleanup,
                close_duplicate=close_duplicate,
                write_fd=write_fd,
                task=task,
            )
