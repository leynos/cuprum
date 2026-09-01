"""Unit integration tests for pump-stream dispatch backend selection.

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
    set_rust_availability_for_testing,
)
from cuprum.unittests._pump_stream_dispatch_support import clear_backend_caches

__all__ = ["clear_backend_caches"]

pytestmark = pytest.mark.usefixtures("clear_backend_caches")


class _DispatchCase(typ.TypedDict):
    """Parametrized backend mode and expected call counts for dispatch tests."""

    backend_env: str
    rust_available: bool | None
    force_fd_extraction_failure: bool
    expected_rust_fd_attempts: int


class TestPumpStreamDispatch:
    """Unit integration tests for ``_pump_stream_dispatch`` selection paths."""

    @pytest.mark.parametrize(
        "case",
        [
            pytest.param(
                _DispatchCase(
                    backend_env="python",
                    rust_available=None,
                    force_fd_extraction_failure=False,
                    expected_rust_fd_attempts=0,
                ),
                id="forced-python",
            ),
            pytest.param(
                _DispatchCase(
                    backend_env="rust",
                    rust_available=True,
                    force_fd_extraction_failure=True,
                    expected_rust_fd_attempts=1,
                ),
                id="rust-fd-extraction-fails",
            ),
        ],
    )
    def test_dispatch_falls_back_to_python(
        self,
        monkeypatch: pytest.MonkeyPatch,
        case: _DispatchCase,
    ) -> None:
        """Python pump is used when forced or when Rust FD extraction fails.

        Parameters
        ----------
        monkeypatch : pytest.MonkeyPatch
            Fixture used to override environment variables.
        case : _DispatchCase
            Parameterized backend mode and expected call counts.
        """
        backend_env = case["backend_env"]
        rust_available = case["rust_available"]
        force_fd_extraction_failure = case["force_fd_extraction_failure"]
        expected_rust_fd_attempts = case["expected_rust_fd_attempts"]
        monkeypatch.setenv("CUPRUM_STREAM_BACKEND", backend_env)
        if rust_available is not None:
            set_rust_availability_for_testing(is_available=rust_available)

        calls = {"rust_fd_path_attempts": 0, "python_pump": 0}

        async def fake_pump(
            reader: asyncio.StreamReader | None,
            writer: asyncio.StreamWriter | None,
        ) -> None:
            """Stand in for the Python pump and record that it ran."""
            del reader, writer
            await asyncio.sleep(0)
            calls["python_pump"] += 1

        def on_rust_fd_path_attempt() -> None:
            """Record that the Rust FD-extraction path was attempted."""
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
        monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
        set_rust_availability_for_testing(is_available=False)

        calls = {"python_pump": 0}

        async def fake_pump(
            reader: asyncio.StreamReader | None,
            writer: asyncio.StreamWriter | None,
        ) -> None:
            """Stand in for the Python pump and record that it ran."""
            del reader, writer
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
