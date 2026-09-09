"""Regression tests for Rust pump cleanup failures."""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import os
import typing as typ
from unittest import mock

import pytest

from cuprum import (
    _pipeline_stream_fds,
    _pipeline_stream_native_cleanup,
    _pipeline_streams,
)
from cuprum.unittests._pump_stream_dispatch_support import (
    _nonblocking_pipe_pair,
    _run_with_inline_executor,
    clear_backend_caches,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ["clear_backend_caches"]

pytestmark = pytest.mark.usefixtures("clear_backend_caches")

_DRAIN_FAILURE_MESSAGE = "drain failed"
_NATIVE_LOAD_FAILURE_MESSAGE = "native extension unavailable"
_NATIVE_FAILURE_MESSAGE = "native pump failed"


class _DrainBaseException(BaseException):
    """Sentinel error used to verify non-``Exception`` drain cleanup."""


def _install_recording_native_failure(
    monkeypatch: pytest.MonkeyPatch,
    call_order: list[str],
    close_writer: mock.AsyncMock,
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
        del reader_fd
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


class _NativeLoadFailureExecutor:
    """Publish import failures through the native-worker future."""

    def submit(
        self,
        function: cabc.Callable[..., object],
        *args: object,
    ) -> cf.Future[object]:
        """Run native loading and settle its concurrent future."""
        future: cf.Future[object] = cf.Future()
        try:
            result = function(*args)
        except ImportError as error:
            future.set_exception(error)
        else:
            future.set_result(result)
        return future


async def _run_with_native_load_failure(
    awaitable: cabc.Awaitable[object],
) -> None:
    """Run an awaitable with native-load failures published by the executor."""
    with mock.patch.object(
        _pipeline_stream_native_cleanup,
        "_NATIVE_PUMP_EXECUTOR",
        _NativeLoadFailureExecutor(),
    ):
        await awaitable


class TestRustPumpFailures:
    """Regression tests for Rust-pump cleanup paths."""

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
                    handoff=_pipeline_streams._RustPumpHandoff(
                        reader_fd=1,
                        writer_fd=2,
                        cleanup_grace_s=0.5,
                    ),
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
        _install_recording_native_failure(monkeypatch, call_order, close_writer)

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
                            handoff=_pipeline_streams._RustPumpHandoff(
                                reader_fd=read_fd,
                                writer_fd=write_fd,
                                cleanup_grace_s=0.5,
                            ),
                        )
                    )
                )

            assert not os.get_blocking(read_fd), (
                "native failures must restore the reader's original blocking mode"
            )
            assert not os.get_blocking(write_fd), (
                "native failures must restore the writer's original blocking mode"
            )

        assert call_order == ["pause", "drain", "restore", "resume"], (
            "expected native failure cleanup to restore modes before resuming"
        )
        close_writer.assert_not_awaited()

    def test_run_rust_pump_closes_duplicate_after_native_load_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A native-load failure should close every state and native duplicate."""
        duplicated_fds: list[int] = []
        original_dup = os.dup

        def record_dup(fd: int) -> int:
            """Duplicate an FD while retaining its value for the assertion."""
            duplicate = original_dup(fd)
            duplicated_fds.append(duplicate)
            return duplicate

        monkeypatch.setattr(os, "dup", record_dup)

        import cuprum._streams_rs as streams_rs

        def fail_native_load() -> object:
            """Model an unavailable native extension import."""
            raise ImportError(_NATIVE_LOAD_FAILURE_MESSAGE)

        monkeypatch.setattr(streams_rs, "_load_native", fail_native_load)

        with _nonblocking_pipe_pair() as (
            read_fd,
            read_write_fd,
            write_read_fd,
            write_fd,
        ):
            del read_write_fd, write_read_fd
            with pytest.raises(ImportError, match=_NATIVE_LOAD_FAILURE_MESSAGE):
                asyncio.run(
                    _run_with_native_load_failure(
                        _pipeline_streams._run_rust_pump(
                            reader=typ.cast("asyncio.StreamReader", object()),
                            writer=None,
                            handoff=_pipeline_streams._RustPumpHandoff(
                                reader_fd=read_fd,
                                writer_fd=write_fd,
                                cleanup_grace_s=0.5,
                            ),
                        )
                    )
                )

            assert duplicated_fds, (
                "native pumping should create state and native duplicates"
            )
            for duplicated_fd in duplicated_fds:
                with pytest.raises(OSError, match="Bad file descriptor"):
                    os.fstat(duplicated_fd)
