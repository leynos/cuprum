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
