"""Regression tests for Rust pump cleanup failures."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import typing as typ
from unittest import mock

import pytest

from cuprum import _pipeline_stream_fds, _pipeline_streams
from cuprum.unittests._pump_stream_dispatch_support import (
    _nonblocking_pipe_pair,
    _run_with_inline_executor,
    bypass_reader_drain,
    clear_backend_caches,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ["clear_backend_caches"]

pytestmark = pytest.mark.usefixtures("clear_backend_caches")

_DRAIN_FAILURE_MESSAGE = "drain failed"
_NATIVE_LOAD_FAILURE_MESSAGE = "native extension unavailable"
_NATIVE_FAILURE_MESSAGE = "native pump failed"
_LINUX_ONLY = pytest.mark.skipif(
    sys.platform != "linux",
    reason="Linux opens independent worker descriptor descriptions",
)


class _DrainBaseException(BaseException):
    """Sentinel error used to verify non-``Exception`` drain cleanup."""


def _install_recording_native_failure(
    monkeypatch: pytest.MonkeyPatch,
    call_order: list[str],
    close_writer: mock.AsyncMock,
    worker_reader_fds: list[int],
) -> None:
    """Install native-pump failure doubles that record cleanup ordering."""

    def pause_reader(
        reader: asyncio.StreamReader,
    ) -> _pipeline_stream_fds._ReaderPause:
        """Record the pause and provide its matching resume callback."""
        del reader
        call_order.append("pause")
        return _pipeline_stream_fds._ReaderPause(
            may_hand_off=True,
            resume=lambda: call_order.append("resume"),
        )

    async def drain_reader(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        """Record buffer draining without writing stream data."""
        del reader, writer
        call_order.append("drain")
        await asyncio.sleep(0)

    def fail_rust_pump(reader_fd: int, writer_fd: int) -> None:
        """Consume the duplicate descriptor before surfacing a pump error."""
        worker_reader_fds.append(reader_fd)
        os.close(writer_fd)
        raise RuntimeError(_NATIVE_FAILURE_MESSAGE)

    original_restore = _pipeline_stream_fds._restore_stream_fd_blocking

    def restore_stream_fd_blocking(
        *,
        reader_fd: int,
        writer_fd: int,
        reader_was_blocking: bool,
        writer_was_blocking: bool,
    ) -> None:
        """Record restoration while restoring the real descriptor modes."""
        call_order.append("restore")
        original_restore(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            reader_was_blocking=reader_was_blocking,
            writer_was_blocking=writer_was_blocking,
        )

    monkeypatch.setattr(_pipeline_streams, "_pause_reader_transport", pause_reader)
    monkeypatch.setattr(_pipeline_streams, "_drain_reader_buffer", drain_reader)
    monkeypatch.setattr(
        _pipeline_stream_fds,
        "_restore_stream_fd_blocking",
        restore_stream_fd_blocking,
    )
    monkeypatch.setattr(_pipeline_streams, "_close_stream_writer", close_writer)

    import cuprum._streams_rs as streams_rs

    monkeypatch.setattr(streams_rs, "rust_pump_stream", fail_rust_pump)


class TestRustPumpFailures:
    """Cover native-pump setup and worker failure cleanup."""

    def test_run_rust_pump_resumes_reader_when_draining_raises_base_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A drain BaseException should resume the reader and propagate unchanged."""
        call_order: list[str] = []

        def pause_reader(
            reader: asyncio.StreamReader,
        ) -> _pipeline_stream_fds._ReaderPause:
            """Record the pause and provide its matching resume callback."""
            del reader
            call_order.append("pause")
            return _pipeline_stream_fds._ReaderPause(
                may_hand_off=True,
                resume=lambda: call_order.append("resume"),
            )

        async def fail_drain(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter | None,
        ) -> None:
            """Fail while draining buffered stream data."""
            del reader, writer
            call_order.append("drain")
            await asyncio.sleep(0)
            raise _DrainBaseException(_DRAIN_FAILURE_MESSAGE)

        monkeypatch.setattr(_pipeline_streams, "_pause_reader_transport", pause_reader)
        monkeypatch.setattr(_pipeline_streams, "_drain_reader_buffer", fail_drain)

        with pytest.raises(_DrainBaseException, match=_DRAIN_FAILURE_MESSAGE):
            asyncio.run(
                _pipeline_streams._run_rust_pump(
                    reader=typ.cast("asyncio.StreamReader", object()),
                    writer=None,
                    reader_fd=1,
                    writer_fd=2,
                )
            )

        assert call_order == ["pause", "drain", "resume"], (
            "expected the reader to resume after drain failure"
        )

    def test_run_rust_pump_restores_then_resumes_on_native_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A native failure should restore modes, resume, and skip close."""
        call_order: list[str] = []
        close_writer = mock.AsyncMock()
        worker_reader_fds: list[int] = []
        _install_recording_native_failure(
            monkeypatch,
            call_order,
            close_writer,
            worker_reader_fds,
        )

        with _nonblocking_pipe_pair() as (
            read_fd,
            read_write_fd,
            write_read_fd,
            write_fd,
        ):
            del read_write_fd, write_read_fd
            with pytest.raises(RuntimeError, match=_NATIVE_FAILURE_MESSAGE):
                asyncio.run(
                    _run_with_inline_executor(
                        _pipeline_streams._run_rust_pump(
                            reader=typ.cast("asyncio.StreamReader", object()),
                            writer=None,
                            reader_fd=read_fd,
                            writer_fd=write_fd,
                        )
                    )
                )

            assert not os.get_blocking(read_fd), (
                "native failures must restore the reader's original blocking mode"
            )
            assert not os.get_blocking(write_fd), (
                "native failures must restore the writer's original blocking mode"
            )
            assert worker_reader_fds, "native work must receive a worker reader"
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(worker_reader_fds[0])

        assert call_order == ["pause", "drain", "restore", "resume"], (
            "expected native failure cleanup to restore modes before resuming"
        )
        close_writer.assert_not_awaited()

    @_LINUX_ONLY
    def test_run_rust_pump_closes_duplicate_when_native_load_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A submitted shim failure should close both worker descriptors once."""
        _ = self
        worker_fds: list[int] = []
        original_open = os.open

        def record_open(path: str, flags: int) -> int:
            """Record each worker descriptor prepared for native pumping."""
            worker_fd = original_open(path, flags)
            worker_fds.append(worker_fd)
            return worker_fd

        import cuprum._streams_rs as streams_rs

        def fail_native_load() -> typ.NoReturn:
            """Fail before the shim can invoke the native callable."""
            raise ImportError(_NATIVE_LOAD_FAILURE_MESSAGE)

        async def run_with_submitted_native_failure(
            awaitable: cabc.Awaitable[object],
        ) -> None:
            """Accept the worker, then publish its pre-native failure."""
            loop = asyncio.get_running_loop()

            def submit_native_work(
                executor: object,
                function: cabc.Callable[..., object],
                *args: object,
            ) -> asyncio.Future[object]:
                """Return a settled future after executing accepted native work."""
                del executor
                future = loop.create_future()
                try:
                    future.set_result(function(*args))
                except BaseException as exc:  # ruff: ignore[blind-except] - preserve worker errors
                    future.set_exception(exc)
                return future

            with mock.patch.object(
                loop,
                "run_in_executor",
                side_effect=submit_native_work,
            ):
                await awaitable

        monkeypatch.setattr(_pipeline_stream_fds.os, "open", record_open)
        monkeypatch.setattr(streams_rs, "_load_native", fail_native_load)
        bypass_reader_drain(monkeypatch)

        with _nonblocking_pipe_pair() as (
            read_fd,
            read_write_fd,
            write_read_fd,
            write_fd,
        ):
            del read_write_fd, write_read_fd
            with pytest.raises(ImportError, match=_NATIVE_LOAD_FAILURE_MESSAGE):
                asyncio.run(
                    run_with_submitted_native_failure(
                        _pipeline_streams._run_rust_pump(
                            reader=typ.cast("asyncio.StreamReader", object()),
                            writer=None,
                            reader_fd=read_fd,
                            writer_fd=write_fd,
                        )
                    )
                )

            assert len(worker_fds) == 2, (
                "native pumping should create reader and writer worker descriptors"
            )
            for worker_fd in worker_fds:
                with pytest.raises(OSError, match="Bad file descriptor"):
                    os.fstat(worker_fd)
            os.fstat(write_fd)

    @_LINUX_ONLY
    def test_run_rust_pump_closes_duplicate_when_executor_rejects_submission(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A rejected executor submission should close both worker descriptors."""
        _ = self
        caplog.set_level(logging.DEBUG, logger="cuprum._pipeline_streams")
        worker_fds: list[int] = []
        original_open = os.open

        def record_open(path: str, flags: int) -> int:
            """Record worker descriptors while retaining them for assertions."""
            worker_fd = original_open(path, flags)
            worker_fds.append(worker_fd)
            return worker_fd

        monkeypatch.setattr(_pipeline_stream_fds.os, "open", record_open)
        close_duplicate = mock.Mock(wraps=_pipeline_streams._close_rust_writer_fd)
        monkeypatch.setattr(
            _pipeline_streams,
            "_close_rust_writer_fd",
            close_duplicate,
        )

        async def run_with_rejected_executor_submission(
            awaitable: cabc.Awaitable[object],
        ) -> None:
            """Reject submission before a worker can consume the duplicate."""
            loop = asyncio.get_running_loop()

            def reject_native_submission(
                executor: object,
                function: cabc.Callable[..., object],
                *args: object,
            ) -> asyncio.Future[object]:
                """Raise before ``function`` is accepted by the executor."""
                del executor, function, args
                raise RuntimeError(_NATIVE_LOAD_FAILURE_MESSAGE)

            with mock.patch.object(
                loop,
                "run_in_executor",
                side_effect=reject_native_submission,
            ):
                await awaitable

        with _nonblocking_pipe_pair() as (
            read_fd,
            read_write_fd,
            write_read_fd,
            write_fd,
        ):
            del read_write_fd, write_read_fd
            with pytest.raises(RuntimeError, match=_NATIVE_LOAD_FAILURE_MESSAGE):
                asyncio.run(
                    run_with_rejected_executor_submission(
                        _pipeline_streams._run_rust_pump(
                            reader=typ.cast("asyncio.StreamReader", object()),
                            writer=None,
                            reader_fd=read_fd,
                            writer_fd=write_fd,
                        )
                    )
                )

            assert len(worker_fds) == 2, (
                "native pumping should create reader and writer worker descriptors"
            )
            close_duplicate.assert_called_once_with(worker_fds[1])
            for worker_fd in worker_fds:
                with pytest.raises(OSError, match="Bad file descriptor"):
                    os.fstat(worker_fd)
        records = [
            record.__dict__
            for record in caplog.records
            if record.__dict__.get("cuprum_action") == "rust_pump_handoff_failed"
        ]
        assert len(records) == 1, "a rejected submission must produce one diagnostic"
        fields = records[0]
        assert fields["cuprum_phase"] == "executor_submission", (
            "the diagnostic must identify executor submission as the failed phase"
        )
        assert fields["cuprum_outcome"] == "failed", (
            "a rejected executor submission must be recorded as failed"
        )
        assert fields["cuprum_error_type"] == "RuntimeError", (
            "the diagnostic must preserve the executor failure category"
        )
        assert fields["cuprum_errno"] is None, (
            "a non-OS executor failure must not invent an errno"
        )
