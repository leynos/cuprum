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
            raise RuntimeError(_DRAIN_FAILURE_MESSAGE)

        monkeypatch.setattr(_pipeline_streams, "_pause_reader_transport", pause_reader)
        monkeypatch.setattr(_pipeline_streams, "_drain_reader_buffer", fail_drain)

        with pytest.raises(RuntimeError, match=_DRAIN_FAILURE_MESSAGE):
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
        monkeypatch.setattr(
            _pipeline_streams,
            "_set_stream_fds_blocking",
            lambda **_: (True, True),
        )
        monkeypatch.setattr(
            _pipeline_streams,
            "_restore_stream_fd_blocking",
            lambda **_: call_order.append("restore"),
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

        assert call_order == ["pause", "drain", "restore", "resume"], (
            "expected native failure cleanup to restore modes before resuming"
        )
        close_writer.assert_not_awaited()
