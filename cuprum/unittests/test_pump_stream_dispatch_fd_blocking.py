"""Unit integration tests for pump-stream dispatch FD-blocking behaviour.

These tests exercise the Rust dispatch path used by pipeline pumping to verify
file-descriptor blocking toggles, reader transport pause/resume ordering, and
rollback of blocking changes when a toggle fails partway through.
"""

from __future__ import annotations

import asyncio
import os
import typing as typ
from unittest import mock

import pytest

from cuprum import _pipeline_streams
from cuprum._testing import (
    configure_pump_stream_dispatch_for_testing,
    set_rust_availability_for_testing,
)
from cuprum.unittests._pump_stream_dispatch_support import (
    _WRITER_TOGGLE_FAILURE,
    PumpCallCounts,
    _fake_python_fallback,
    _make_blocking_fd_spy,
    _nonblocking_pipe_pair,
    _ReaderWithoutPause,
    _run_with_inline_executor,
    _run_with_inline_executor_returning,
    _WriterWithoutPause,
    bypass_reader_drain,
    clear_backend_caches,
    install_closing_rust_pump,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ["clear_backend_caches"]

pytestmark = pytest.mark.usefixtures("clear_backend_caches")


class TestPumpStreamDispatch:
    """Unit integration tests for ``_pump_stream_dispatch`` FD toggling."""

    def test_dispatch_sets_rust_fds_blocking_before_native_pump(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rust dispatch toggles FDs to blocking only during native pumping.

        Parameters
        ----------
        monkeypatch : pytest.MonkeyPatch
            Fixture used to override environment variables and internals.
        """
        _ = self
        monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
        set_rust_availability_for_testing(is_available=True)

        original_reader_blocking = True
        original_writer_blocking = True
        with _nonblocking_pipe_pair() as (
            read_fd,
            read_write_fd,
            write_read_fd,
            write_fd,
        ):
            del read_write_fd, write_read_fd
            reader = typ.cast("asyncio.StreamReader", object())
            writer = mock.MagicMock(spec=asyncio.StreamWriter)
            writer.wait_closed = mock.AsyncMock()
            monkeypatch.setattr(
                _pipeline_streams,
                "_extract_stream_fd",
                lambda stream: (
                    read_fd
                    if stream is reader
                    else write_fd
                    if stream is writer
                    else None
                ),
            )

            calls: PumpCallCounts = {"rust_pump": 0, "python_pump": 0}

            async def fake_python_pump(
                reader: asyncio.StreamReader | None,
                writer: asyncio.StreamWriter | None,
            ) -> None:
                """Stand in for the Python pump and record that it ran."""
                await asyncio.sleep(0)
                calls["python_pump"] += 1

            import cuprum._streams_rs as streams_rs

            monkeypatch.setattr(
                streams_rs,
                "rust_pump_stream",
                _make_blocking_fd_spy(calls, read_fd, write_fd),
            )
            configure_pump_stream_dispatch_for_testing(python_pump=fake_python_pump)

            asyncio.run(
                _run_with_inline_executor(
                    _pipeline_streams._pump_stream_dispatch(reader, writer)
                )
            )
            original_reader_blocking = _pipeline_streams.os.get_blocking(read_fd)
            original_writer_blocking = _pipeline_streams.os.get_blocking(write_fd)
            asyncio.run(_pipeline_streams._pump_stream_dispatch(reader, None))

        assert calls["rust_pump"] == 1, "expected Rust pump path to execute once"
        assert calls["python_pump"] == 1, (
            "expected Python fallback when no writer is available"
        )
        assert not original_reader_blocking, (
            "expected original reader FD to remain non-blocking"
        )
        assert not original_writer_blocking, (
            "expected original writer FD to remain non-blocking"
        )

    def test_run_rust_pump_pauses_reader_before_draining(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rust pumping should pause the reader transport before draining."""
        _ = self
        call_order: list[str] = []

        def fake_pause_reader_transport(
            reader: asyncio.StreamReader,
        ) -> cabc.Callable[[], None]:
            """Record a pause and return a resume callback for the reader."""
            del reader
            call_order.append("pause")

            def _resume() -> None:
                """Record that the reader transport was resumed."""
                call_order.append("resume")

            return _resume

        async def fake_drain_reader_buffer(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter | None,
        ) -> None:
            """Record that the reader buffer was drained."""
            del reader, writer
            await asyncio.sleep(0)
            call_order.append("drain")

        monkeypatch.setattr(
            _pipeline_streams,
            "_pause_reader_transport",
            fake_pause_reader_transport,
        )
        monkeypatch.setattr(
            _pipeline_streams,
            "_drain_reader_buffer",
            fake_drain_reader_buffer,
        )
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

        import cuprum._streams_rs as streams_rs

        def consuming_rust_pump(reader_fd: int, writer_fd: int) -> int:
            """Consume the duplicate writer FD exactly as the Rust pump does."""
            del reader_fd
            os.close(writer_fd)
            return 0

        monkeypatch.setattr(streams_rs, "rust_pump_stream", consuming_rust_pump)

        reader = typ.cast("asyncio.StreamReader", object())
        asyncio.run(
            _run_with_inline_executor(
                _pipeline_streams._run_rust_pump(
                    reader=reader,
                    writer=None,
                    reader_fd=1,
                    writer_fd=2,
                )
            )
        )

        assert call_order == ["pause", "drain", "restore", "resume"]

    def test_dispatch_restores_reader_blocking_when_writer_toggle_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A writer toggle failure must roll back any reader blocking change."""
        _ = self
        monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
        set_rust_availability_for_testing(is_available=True)

        with _nonblocking_pipe_pair() as (
            read_fd,
            read_write_fd,
            write_read_fd,
            write_fd,
        ):
            del read_write_fd, write_read_fd
            reader = typ.cast("asyncio.StreamReader", object())
            writer = typ.cast("asyncio.StreamWriter", object())
            monkeypatch.setattr(
                _pipeline_streams,
                "_extract_stream_fd",
                lambda stream: read_fd if stream is reader else write_fd,
            )

            original_set_blocking = _pipeline_streams.os.set_blocking
            calls: PumpCallCounts = {"python_pump": 0}

            def fake_set_blocking(fd: int, blocking: object) -> None:
                """Toggle blocking but fail when the writer FD is set blocking.

                Raises
                ------
                OSError
                    If the writer FD is switched to blocking mode, simulating
                    a failure partway through the toggle sequence.
                """
                if fd == read_fd and blocking is True:
                    original_set_blocking(
                        fd,
                        True,  # noqa: FBT003  # mirrors os API here
                    )
                    return
                # Match on the writer role rather than a fixed descriptor.
                if fd != read_fd and blocking is True:
                    raise OSError(_WRITER_TOGGLE_FAILURE)
                original_set_blocking(fd, bool(blocking))

            monkeypatch.setattr(_pipeline_streams.os, "set_blocking", fake_set_blocking)
            configure_pump_stream_dispatch_for_testing(
                python_pump=lambda reader, writer: _fake_python_fallback(
                    reader,
                    writer,
                    calls,
                )
            )

            asyncio.run(_pipeline_streams._pump_stream_dispatch(reader, writer))

            assert calls["python_pump"] == 1, (
                "expected Python fallback when writer blocking toggle fails"
            )
            assert not _pipeline_streams.os.get_blocking(read_fd), (
                "expected reader FD blocking mode to be restored after fallback"
            )
            assert not _pipeline_streams.os.get_blocking(write_fd), (
                "expected writer FD to remain in its original non-blocking mode"
            )

    def test_dispatch_uses_rust_when_reader_transport_cannot_pause(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing pause/resume hooks should not force a Python fallback."""
        _ = self
        monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
        set_rust_availability_for_testing(is_available=True)

        with _nonblocking_pipe_pair() as (
            read_fd,
            read_write_fd,
            write_read_fd,
            write_fd,
        ):
            del read_write_fd, write_read_fd
            calls: PumpCallCounts = {"rust_pump": 0, "python_pump": 0}

            def fake_rust_pump_stream(reader_fd: int, writer_fd: int) -> int:
                """Stand in for the Rust pump and record that it ran.

                Returns
                -------
                int
                    Always ``0`` to mimic a successful native pump.
                """
                assert reader_fd == read_fd, "expected the extracted reader FD"
                # Rust consumes its writer descriptor, so it receives a
                # duplicate and closes it; the transport FD stays asyncio's.
                assert writer_fd != write_fd, (
                    "expected a duplicate rather than the transport writer FD"
                )
                calls["rust_pump"] += 1
                os.close(writer_fd)
                return 0

            import cuprum._streams_rs as streams_rs

            monkeypatch.setattr(
                streams_rs,
                "rust_pump_stream",
                fake_rust_pump_stream,
            )
            monkeypatch.setattr(
                _pipeline_streams,
                "_close_stream_writer",
                lambda _writer: asyncio.sleep(0),
            )
            configure_pump_stream_dispatch_for_testing(
                python_pump=lambda reader, writer: _fake_python_fallback(
                    reader,
                    writer,
                    calls,
                )
            )

            reader = typ.cast(
                "asyncio.StreamReader",
                _ReaderWithoutPause(read_fd),
            )
            writer = typ.cast(
                "asyncio.StreamWriter",
                _WriterWithoutPause(write_fd),
            )
            asyncio.run(
                _run_with_inline_executor(
                    _pipeline_streams._pump_stream_dispatch(reader, writer)
                )
            )

            assert calls["rust_pump"] == 1, (
                "expected Rust path even when reader transport lacks pause hooks"
            )
        assert calls["python_pump"] == 0, (
            "did not expect Python fallback when Rust pump succeeds"
        )

    def test_rust_pump_receives_a_duplicate_not_the_transport_fd(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rust consumes a duplicate, leaving the transport descriptor intact.

        The double closes what it receives, as ``rust_pump_stream`` does, so
        handing over the transport's own descriptor would surface as ``EBADF``.

        Parameters
        ----------
        monkeypatch : pytest.MonkeyPatch
            Fixture used to override the native pump and FD extraction.
        """
        _ = self
        received = install_closing_rust_pump(monkeypatch)
        bypass_reader_drain(monkeypatch)

        with _nonblocking_pipe_pair() as (
            read_fd,
            read_write_fd,
            write_read_fd,
            write_fd,
        ):
            del read_write_fd, write_read_fd
            reader = typ.cast("asyncio.StreamReader", object())
            writer = mock.MagicMock(spec=asyncio.StreamWriter)
            writer.wait_closed = mock.AsyncMock()

            handled = asyncio.run(
                _run_with_inline_executor_returning(
                    _pipeline_streams._run_rust_pump(
                        reader=reader,
                        writer=writer,
                        reader_fd=read_fd,
                        writer_fd=write_fd,
                    )
                )
            )

            assert handled is True, "expected the native pump path to report success"
            assert received["writer_fd"] != write_fd, (
                "Rust must receive a duplicate, never the transport's descriptor"
            )
            try:  # the duplicate's close must not take the original with it
                os.fstat(write_fd)
            except OSError as exc:  # pragma: no cover - failure path only
                pytest.fail(
                    f"transport writer FD must stay valid after the native "
                    f"pump closed its duplicate, got {exc!r}"
                )
            assert writer.close.called, (
                "the asyncio writer must still be closed to signal EOF"
            )
