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

    @pytest.mark.parametrize(
        "case",
        [
            pytest.param(
                {
                    "backend_env": "python",
                    "rust_available": None,
                    "force_fd_extraction_failure": False,
                    "expected_rust_fd_attempts": 0,
                },
                id="forced-python",
            ),
            pytest.param(
                {
                    "backend_env": "rust",
                    "rust_available": True,
                    "force_fd_extraction_failure": True,
                    "expected_rust_fd_attempts": 1,
                },
                id="rust-fd-extraction-fails",
            ),
        ],
    )
    def test_dispatch_falls_back_to_python(
        self,
        monkeypatch: pytest.MonkeyPatch,
        case: dict[str, str | bool | int | None],
    ) -> None:
        """Python pump is used when forced or when Rust FD extraction fails.

        Parameters
        ----------
        monkeypatch : pytest.MonkeyPatch
            Fixture used to override environment variables.
        case : dict[str, str | bool | None | int]
            Parameterized backend mode and expected call counts.
        """
        _ = self
        backend_env = typ.cast("str", case["backend_env"])
        rust_available = typ.cast("bool | None", case["rust_available"])
        force_fd_extraction_failure = typ.cast(
            "bool", case["force_fd_extraction_failure"]
        )
        expected_rust_fd_attempts = typ.cast("int", case["expected_rust_fd_attempts"])
        monkeypatch.setenv("CUPRUM_STREAM_BACKEND", backend_env)
        if rust_available is not None:
            set_rust_availability_for_testing(is_available=rust_available)

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
            force_fd_extraction_failure=force_fd_extraction_failure,
            on_rust_fd_path_attempt=on_rust_fd_path_attempt,
            python_pump=fake_pump,
        )

        reader = typ.cast("asyncio.StreamReader", object())
        writer = typ.cast("asyncio.StreamWriter", object())
        asyncio.run(_pipeline_streams._pump_stream_dispatch(reader, writer))

        assert calls["rust_fd_path_attempts"] == expected_rust_fd_attempts, (
            f"expected {expected_rust_fd_attempts} Rust FD extraction attempt(s), "
            f"got {calls['rust_fd_path_attempts']}"
        )
        assert calls["python_pump"] == 1, "expected Python pump to handle the dispatch"

    @staticmethod
    def _create_nonblocking_pipe_pair() -> tuple[int, int, int, int]:
        """Create two OS pipes with their active ends set to non-blocking."""
        read_fd, read_write_fd = _pipeline_streams.os.pipe()
        write_read_fd, write_fd = _pipeline_streams.os.pipe()
        _pipeline_streams.os.set_blocking(read_fd, False)
        _pipeline_streams.os.set_blocking(write_fd, False)
        return read_fd, read_write_fd, write_read_fd, write_fd

    @staticmethod
    def _make_blocking_fd_spy(
        calls: dict[str, int],
        expected_reader_fd: int,
        expected_writer_fd: int,
    ) -> "typ.Callable[[int, int], int]":  # noqa: UP037
        """Return a fake ``rust_pump_stream`` that asserts FDs are blocking."""

        def _spy(reader_fd: int, writer_fd: int) -> int:
            assert _pipeline_streams.os.get_blocking(reader_fd), (
                "expected reader FD to be switched to blocking mode"
            )
            assert _pipeline_streams.os.get_blocking(writer_fd), (
                "expected writer FD to be switched to blocking mode"
            )
            assert reader_fd == expected_reader_fd, (
                "expected Rust path to use extracted reader FD"
            )
            assert writer_fd == expected_writer_fd, (
                "expected Rust path to use extracted writer FD"
            )
            calls["rust_pump"] += 1
            return 0

        return _spy

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

        read_fd, read_write_fd, write_read_fd, write_fd = (
            self._create_nonblocking_pipe_pair()
        )
        original_reader_blocking = True
        original_writer_blocking = True
        try:
            monkeypatch.setattr(
                _pipeline_streams, "_extract_reader_fd", lambda _: read_fd
            )
            monkeypatch.setattr(
                _pipeline_streams, "_extract_writer_fd", lambda _: write_fd
            )

            calls = {"rust_pump": 0, "python_pump": 0}

            async def fake_python_pump(
                reader: asyncio.StreamReader | None,
                writer: asyncio.StreamWriter | None,
            ) -> None:
                await asyncio.sleep(0)
                calls["python_pump"] += 1

            import cuprum._streams_rs as streams_rs

            monkeypatch.setattr(
                streams_rs,
                "rust_pump_stream",
                self._make_blocking_fd_spy(calls, read_fd, write_fd),
            )
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
