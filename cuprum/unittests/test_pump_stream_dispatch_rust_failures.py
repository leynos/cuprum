"""Regression tests for Rust pump cleanup failures."""

from __future__ import annotations

import asyncio
import os
import typing as typ
from unittest import mock

import pytest

from cuprum import _pipeline_streams
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


class TestRustPumpFailures:
    """Regression tests for Rust-pump cleanup paths."""

    def test_run_rust_pump_resumes_reader_when_draining_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A drain error should resume the reader and propagate unchanged."""
        _ = self
        call_order: list[str] = []

        def pause_reader(
            reader: asyncio.StreamReader,
        ) -> cabc.Callable[[], None]:
            """Record the pause and provide its matching resume callback."""
            del reader
            call_order.append("pause")
            return lambda: call_order.append("resume")

        async def fail_drain(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter | None,
        ) -> None:
            """Fail while draining buffered stream data."""
            del reader, writer
            call_order.append("drain")
            await asyncio.sleep(0)
            raise OSError(_DRAIN_FAILURE_MESSAGE)

        monkeypatch.setattr(_pipeline_streams, "_pause_reader_transport", pause_reader)
        monkeypatch.setattr(_pipeline_streams, "_drain_reader_buffer", fail_drain)

        with pytest.raises(OSError, match=_DRAIN_FAILURE_MESSAGE):
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
        _ = self
        call_order: list[str] = []
        close_writer = mock.AsyncMock()

        def pause_reader(
            reader: asyncio.StreamReader,
        ) -> cabc.Callable[[], None]:
            """Record the pause and provide its matching resume callback."""
            del reader
            call_order.append("pause")
            return lambda: call_order.append("resume")

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

        monkeypatch.setattr(_pipeline_streams, "_pause_reader_transport", pause_reader)
        monkeypatch.setattr(_pipeline_streams, "_drain_reader_buffer", drain_reader)
        original_restore = _pipeline_streams._restore_stream_fd_blocking

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

        monkeypatch.setattr(
            _pipeline_streams,
            "_restore_stream_fd_blocking",
            restore_stream_fd_blocking,
        )
        monkeypatch.setattr(_pipeline_streams, "_close_stream_writer", close_writer)

        import cuprum._streams_rs as streams_rs

        monkeypatch.setattr(streams_rs, "rust_pump_stream", fail_rust_pump)

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

        assert call_order == ["pause", "drain", "restore", "resume"], (
            "expected native failure cleanup to restore modes before resuming"
        )
        close_writer.assert_not_awaited()

    def test_run_rust_pump_closes_duplicate_after_native_load_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A native-load failure should close the duplicate writer descriptor."""
        _ = self
        duplicated_fds: list[int] = []
        original_dup = os.dup

        def record_dup(fd: int) -> int:
            """Duplicate an FD while retaining its value for the assertion."""
            duplicate = original_dup(fd)
            duplicated_fds.append(duplicate)
            return duplicate

        monkeypatch.setattr(_pipeline_streams.os, "dup", record_dup)

        import cuprum._streams_rs as streams_rs

        def fail_native_load() -> object:
            """Model an unavailable native extension import."""
            raise ImportError(_NATIVE_LOAD_FAILURE_MESSAGE)

        async def run_with_native_load_failure(
            awaitable: cabc.Awaitable[object],
        ) -> None:
            """Publish a native-load failure through the submitted future."""
            loop = asyncio.get_running_loop()

            def submit_native_load_failure(
                executor: object,
                function: cabc.Callable[..., object],
                *args: object,
            ) -> asyncio.Future[object]:
                """Execute the worker and publish its import failure to a future."""
                del executor
                future = loop.create_future()
                try:
                    function(*args)
                except ImportError as exc:
                    future.set_exception(exc)
                return future

            with mock.patch.object(
                loop,
                "run_in_executor",
                side_effect=submit_native_load_failure,
            ):
                await awaitable

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
                    run_with_native_load_failure(
                        _pipeline_streams._run_rust_pump(
                            reader=typ.cast("asyncio.StreamReader", object()),
                            writer=None,
                            reader_fd=read_fd,
                            writer_fd=write_fd,
                        )
                    )
                )

            assert duplicated_fds, "native pumping should create a writer duplicate"
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(duplicated_fds[0])

    def test_run_rust_pump_defers_duplicate_close_until_worker_settles(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cancellation should leave the native writer FD live until completion."""
        _ = self

        async def run_case() -> None:
            """Cancel a pump task while its submitted native work remains pending."""
            loop = asyncio.get_running_loop()
            native_pump = loop.create_future()
            received_fds: list[int] = []

            def retain_native_writer(reader_fd: int, writer_fd: int) -> int:
                """Record the descriptor while modelling work owned by the future."""
                del reader_fd
                received_fds.append(writer_fd)
                return 0

            def submit_held_native_pump(
                executor: object,
                function: object,
                *args: object,
            ) -> asyncio.Future[object]:
                """Start native work but delay publication of its completion."""
                del executor
                typ.cast("cabc.Callable[..., object]", function)(*args)
                return native_pump

            import cuprum._streams_rs as streams_rs

            monkeypatch.setattr(
                streams_rs,
                "rust_pump_stream",
                retain_native_writer,
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
                    side_effect=submit_held_native_pump,
                ):
                    task = asyncio.create_task(
                        _pipeline_streams._run_rust_pump(
                            reader=typ.cast("asyncio.StreamReader", object()),
                            writer=None,
                            reader_fd=read_fd,
                            writer_fd=write_fd,
                        )
                    )
                    await asyncio.sleep(0)
                    task.cancel()

                    with pytest.raises(asyncio.CancelledError):
                        await task

                    assert received_fds, "native work should receive a duplicate FD"
                    os.fstat(received_fds[0])

                    native_pump.set_result(0)
                    await asyncio.sleep(0)

                    with pytest.raises(OSError, match="Bad file descriptor"):
                        os.fstat(received_fds[0])

        asyncio.run(run_case())
