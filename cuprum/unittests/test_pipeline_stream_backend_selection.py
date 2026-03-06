"""Unit integration tests for pipeline stream backend pathway selection.

These tests exercise the dispatch layer used by pipeline pumping to verify
backend overrides, forced fallback to Python, and forced-rust error handling.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import _pipeline_streams
from cuprum._testing import (
    configure_pump_stream_dispatch_for_testing,
    reset_pump_stream_dispatch_for_testing,
    set_rust_availability_for_testing,
)

pytestmark = pytest.mark.usefixtures("clear_backend_caches")


@pytest.fixture
def clear_backend_caches() -> typ.Iterator[None]:
    """Clear and restore backend-selection test hooks for each test."""
    from cuprum import _backend

    set_rust_availability_for_testing(is_available=None)
    reset_pump_stream_dispatch_for_testing()
    _backend._check_rust_available.cache_clear()
    _backend.get_stream_backend.cache_clear()
    yield
    set_rust_availability_for_testing(is_available=None)
    reset_pump_stream_dispatch_for_testing()
    _backend._check_rust_available.cache_clear()
    _backend.get_stream_backend.cache_clear()


class TestPumpStreamDispatch:
    """Unit integration tests for ``_pump_stream_dispatch`` selection paths."""

    def test_dispatch_uses_python_when_forced(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Forced Python backend routes dispatch directly to Python pump.

        Parameters
        ----------
        monkeypatch : pytest.MonkeyPatch
            Fixture used to override environment variables.
        """
        _ = self
        monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "python")
        calls = {"rust_fd_path_attempts": 0, "python_pump": 0}

        async def fake_pump(
            reader: asyncio.StreamReader | None,
            writer: asyncio.StreamWriter | None,
        ) -> None:
            await asyncio.sleep(0)
            calls["python_pump"] += 1

        def on_rust_fd_path_attempt() -> None:
            calls["rust_fd_path_attempts"] += 1

        configure_pump_stream_dispatch_for_testing(
            on_rust_fd_path_attempt=on_rust_fd_path_attempt,
            python_pump=fake_pump,
        )

        reader = typ.cast("asyncio.StreamReader", object())
        writer = typ.cast("asyncio.StreamWriter", object())
        asyncio.run(_pipeline_streams._pump_stream_dispatch(reader, writer))

        assert calls["rust_fd_path_attempts"] == 0, (
            "did not expect Rust FD extraction attempt in forced Python mode"
        )
        assert calls["python_pump"] == 1, (
            "expected Python pump to handle forced Python mode"
        )

    def test_dispatch_falls_back_to_python_when_rust_fd_extraction_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Forced Rust mode falls back to Python when FD extraction fails.

        Parameters
        ----------
        monkeypatch : pytest.MonkeyPatch
            Fixture used to override environment variables.
        """
        _ = self
        monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
        set_rust_availability_for_testing(is_available=True)
        calls = {"rust_fd_path_attempts": 0, "python_pump": 0}

        async def fake_pump(
            reader: asyncio.StreamReader | None,
            writer: asyncio.StreamWriter | None,
        ) -> None:
            await asyncio.sleep(0)
            calls["python_pump"] += 1

        def on_rust_fd_path_attempt() -> None:
            calls["rust_fd_path_attempts"] += 1

        configure_pump_stream_dispatch_for_testing(
            force_fd_extraction_failure=True,
            on_rust_fd_path_attempt=on_rust_fd_path_attempt,
            python_pump=fake_pump,
        )

        reader = typ.cast("asyncio.StreamReader", object())
        writer = typ.cast("asyncio.StreamWriter", object())
        asyncio.run(_pipeline_streams._pump_stream_dispatch(reader, writer))

        assert calls["rust_fd_path_attempts"] == 1, (
            "expected Rust FD extraction path to be attempted once"
        )
        assert calls["python_pump"] == 1, (
            "expected Python pump fallback when Rust FD extraction is unavailable"
        )

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

        read_fd, read_write_fd = _pipeline_streams.os.pipe()
        write_read_fd, write_fd = _pipeline_streams.os.pipe()
        _pipeline_streams.os.set_blocking(read_fd, False)
        _pipeline_streams.os.set_blocking(write_fd, False)
        original_reader_blocking = True
        original_writer_blocking = True
        try:
            monkeypatch.setattr(
                _pipeline_streams,
                "_extract_reader_fd",
                lambda _: read_fd,
            )
            monkeypatch.setattr(
                _pipeline_streams,
                "_extract_writer_fd",
                lambda _: write_fd,
            )

            calls = {"rust_pump": 0, "python_pump": 0}

            async def fake_python_pump(
                reader: asyncio.StreamReader | None,
                writer: asyncio.StreamWriter | None,
            ) -> None:
                await asyncio.sleep(0)
                calls["python_pump"] += 1

            import cuprum._streams_rs as streams_rs

            def fake_rust_pump(reader_fd: int, writer_fd: int) -> int:
                assert _pipeline_streams.os.get_blocking(reader_fd), (
                    "expected reader FD to be switched to blocking mode"
                )
                assert _pipeline_streams.os.get_blocking(writer_fd), (
                    "expected writer FD to be switched to blocking mode"
                )
                assert reader_fd == read_fd, (
                    "expected Rust path to use extracted reader FD"
                )
                assert writer_fd == write_fd, (
                    "expected Rust path to use extracted writer FD"
                )
                calls["rust_pump"] += 1
                return 0

            monkeypatch.setattr(streams_rs, "rust_pump_stream", fake_rust_pump)
            configure_pump_stream_dispatch_for_testing(python_pump=fake_python_pump)

            reader = typ.cast("asyncio.StreamReader", object())
            asyncio.run(_pipeline_streams._pump_stream_dispatch(reader, None))
            original_reader_blocking = _pipeline_streams.os.get_blocking(read_fd)
            original_writer_blocking = _pipeline_streams.os.get_blocking(write_fd)
        finally:
            _pipeline_streams.os.close(read_fd)
            _pipeline_streams.os.close(read_write_fd)
            _pipeline_streams.os.close(write_read_fd)
            _pipeline_streams.os.close(write_fd)

        assert calls["rust_pump"] == 1, "expected Rust pump path to execute once"
        assert calls["python_pump"] == 0, (
            "did not expect Python fallback when Rust pump succeeds"
        )
        assert not original_reader_blocking, (
            "expected original reader FD to remain non-blocking"
        )
        assert not original_writer_blocking, (
            "expected original writer FD to remain non-blocking"
        )

    def test_dispatch_raises_import_error_when_rust_forced_but_unavailable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Forced Rust mode surfaces ImportError when extension is unavailable.

        Parameters
        ----------
        monkeypatch : pytest.MonkeyPatch
            Fixture used to override environment variables.
        """
        _ = self
        monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
        set_rust_availability_for_testing(is_available=False)

        calls = {"python_pump": 0}

        async def fake_pump(
            reader: asyncio.StreamReader | None,
            writer: asyncio.StreamWriter | None,
        ) -> None:
            await asyncio.sleep(0)
            calls["python_pump"] += 1

        configure_pump_stream_dispatch_for_testing(python_pump=fake_pump)

        reader = typ.cast("asyncio.StreamReader", object())
        writer = typ.cast("asyncio.StreamWriter", object())
        with pytest.raises(ImportError, match="CUPRUM_STREAM_BACKEND"):
            asyncio.run(_pipeline_streams._pump_stream_dispatch(reader, writer))

        assert calls["python_pump"] == 0, (
            "Python pump should not run when forced Rust is unavailable"
        )
