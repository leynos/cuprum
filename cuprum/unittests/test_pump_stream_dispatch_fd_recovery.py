"""Regression tests for failed native-pump FD setup."""

from __future__ import annotations

import asyncio
import os
import typing as typ

import pytest

from cuprum import _pipeline_streams
from cuprum._testing import (
    configure_pump_stream_dispatch_for_testing,
    set_rust_availability_for_testing,
)
from cuprum.unittests._pump_stream_dispatch_support import (
    PumpCallCounts,
    _fake_python_fallback,
    _nonblocking_pipe_pair,
    clear_backend_caches,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ["clear_backend_caches"]

pytestmark = pytest.mark.usefixtures("clear_backend_caches")

_WRITER_TOGGLE_VALUE_ERROR = "writer toggle value invalid"


def _install_value_error_recovery_doubles(
    monkeypatch: pytest.MonkeyPatch,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    read_fd: int,
    write_fd: int,
    resume_calls: list[str],
    calls: PumpCallCounts,
) -> None:
    """Install the test doubles for writer-blocking recovery."""

    def pause_reader(
        paused_reader: asyncio.StreamReader,
    ) -> cabc.Callable[[], None]:
        """Return a callback that records reader transport recovery."""
        del paused_reader

        def resume_reader() -> None:
            """Record reader transport recovery after fallback."""
            resume_calls.append("resume")

        return resume_reader

    async def no_drain(
        drain_reader: asyncio.StreamReader,
        drain_writer: asyncio.StreamWriter | None,
    ) -> None:
        """Avoid buffering concerns while exercising descriptor recovery."""
        del drain_reader, drain_writer
        await asyncio.sleep(0)

    original_set_blocking = os.set_blocking

    def fail_writer_toggle(fd: int, is_blocking: object) -> None:
        """Change the reader mode then reject the writer mode change."""
        if fd == read_fd and is_blocking is True:
            original_set_blocking(fd, bool(is_blocking))
            return
        if fd == write_fd and is_blocking is True:
            raise ValueError(_WRITER_TOGGLE_VALUE_ERROR)
        original_set_blocking(fd, bool(is_blocking))

    monkeypatch.setattr(_pipeline_streams, "_pause_reader_transport", pause_reader)
    monkeypatch.setattr(_pipeline_streams, "_drain_reader_buffer", no_drain)
    monkeypatch.setattr(
        _pipeline_streams,
        "_extract_stream_fd",
        lambda stream: read_fd if stream is reader else write_fd,
    )
    monkeypatch.setattr(_pipeline_streams.os, "set_blocking", fail_writer_toggle)
    configure_pump_stream_dispatch_for_testing(
        python_pump=lambda fallback_reader, fallback_writer: _fake_python_fallback(
            fallback_reader,
            fallback_writer,
            calls,
        )
    )


class TestPumpStreamFdRecovery:
    """Regression tests for Python fallback after FD setup failures."""

    def test_dispatch_falls_back_after_value_error_and_resumes_reader(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ValueError during writer setup should roll back and use Python pumping."""
        _ = self
        monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
        set_rust_availability_for_testing(is_available=True)
        resume_calls: list[str] = []
        calls: PumpCallCounts = {"python_pump": 0}

        with _nonblocking_pipe_pair() as (
            read_fd,
            read_write_fd,
            write_read_fd,
            write_fd,
        ):
            del read_write_fd, write_read_fd
            reader = typ.cast("asyncio.StreamReader", object())
            writer = typ.cast("asyncio.StreamWriter", object())
            _install_value_error_recovery_doubles(
                monkeypatch,
                reader,
                writer,
                read_fd,
                write_fd,
                resume_calls,
                calls,
            )

            asyncio.run(_pipeline_streams._pump_stream_dispatch(reader, writer))

            assert calls["python_pump"] == 1, (
                "expected Python fallback after invalid writer blocking mode"
            )
            assert resume_calls == ["resume"], (
                "expected the paused reader transport to resume after fallback"
            )
            assert not os.get_blocking(read_fd), (
                "expected reader blocking mode rollback after setup failure"
            )
            assert not os.get_blocking(write_fd), (
                "expected writer blocking mode to remain unchanged after setup failure"
            )
