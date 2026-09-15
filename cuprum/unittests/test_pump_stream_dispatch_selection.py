"""Unit integration tests for pump-stream dispatch backend selection.

These tests exercise the dispatch layer used by pipeline pumping to verify
backend overrides, forced fallback to Python, and forced-rust error handling.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import _pipeline_streams
from cuprum._pipeline_stream_fds import _ReaderPause
from cuprum._testing import (
    configure_pump_stream_dispatch_for_testing,
    set_rust_availability_for_testing,
)
from cuprum.pump_events import PumpEvent, RustPumpDeclineReason
from cuprum.pump_observation import observe_pump
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
        monkeypatch.setattr(
            _pipeline_streams,
            "_native_pump_supported_on_platform",
            lambda: True,
        )
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

    def test_dispatch_declines_unsupported_native_platform_before_rust_work(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unsupported platforms fall back without touching native-pump seams."""
        monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
        monkeypatch.setattr(
            _pipeline_streams,
            "_native_pump_supported_on_platform",
            lambda: False,
        )
        set_rust_availability_for_testing(is_available=True)
        calls = {"python_pump": 0}
        events: list[PumpEvent] = []

        async def fake_pump(
            reader: asyncio.StreamReader | None,
            writer: asyncio.StreamWriter | None,
        ) -> None:
            """Record the fallback pump invocation."""
            del reader, writer
            calls["python_pump"] += 1
            await asyncio.sleep(0)

        def unexpected_rust_work() -> None:
            """Fail if platform rejection reaches the Rust FD path."""
            msg = "unsupported platforms must not start the Rust FD path"
            raise AssertionError(msg)

        def unexpected_raw_fd_extraction(
            stream: asyncio.StreamReader | asyncio.StreamWriter | None,
        ) -> int | None:
            """Fail if platform rejection reaches raw descriptor extraction."""
            del stream
            msg = "unsupported platforms must not extract raw descriptors"
            raise AssertionError(msg)

        configure_pump_stream_dispatch_for_testing(
            on_rust_fd_path_attempt=unexpected_rust_work,
            raw_fd_extractor=unexpected_raw_fd_extraction,
            python_pump=fake_pump,
        )
        reader = typ.cast("asyncio.StreamReader", object())
        writer = typ.cast("asyncio.StreamWriter", object())
        with observe_pump(events.append):
            asyncio.run(_pipeline_streams._pump_stream_dispatch(reader, writer))

        assert calls["python_pump"] == 1, "the Python pump must handle fallback"
        assert events == [
            PumpEvent(
                phase="declined",
                reason=RustPumpDeclineReason.PLATFORM_UNSUPPORTED,
            )
        ], "platform rejection must emit one bounded decline event"

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

    def test_dispatch_forwards_explicit_read_size_to_python_pump(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The pipeline dispatch seam forwards a benchmark read size unchanged."""
        monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "python")
        observed: list[int] = []

        async def fake_pump(
            reader: asyncio.StreamReader | None,
            writer: asyncio.StreamWriter | None,
            *,
            read_size: int,
        ) -> None:
            """Record the dispatched size without touching stream endpoints."""
            del reader, writer
            observed.append(read_size)
            await asyncio.sleep(0)

        monkeypatch.setattr(_pipeline_streams, "_pump_stream", fake_pump)
        reader = typ.cast("asyncio.StreamReader", object())
        writer = typ.cast("asyncio.StreamWriter", object())

        asyncio.run(
            _pipeline_streams._pump_stream_dispatch(
                reader,
                writer,
                read_size=17,
            )
        )

        assert observed == [17], (
            "the dispatch seam must retain the explicit benchmark read size, "
            f"got {observed}"
        )


class TestTestOwnedDescriptorHandoff:
    """A closing-reader decline yields only to the test-owned descriptor seam."""

    @staticmethod
    def _closing_pause() -> _ReaderPause:
        """Return the decline a closing reader transport produces."""
        return _ReaderPause(
            decline_reason=RustPumpDeclineReason.READER_PAUSE_FAILED,
            closing_transport=True,
        )

    def test_closing_pause_stays_declined_without_a_test_extractor(self) -> None:
        """A real hand-off keeps refusing a reader whose transport is closing."""
        assert (
            _pipeline_streams._PUMP_STREAM_DISPATCH_TEST_HOOKS.raw_fd_extractor is None
        ), "this case describes the unstubbed descriptor supply"
        closing = self._closing_pause()

        permitted = _pipeline_streams._permit_test_owned_descriptor_handoff(closing)

        assert permitted is closing, (
            "without a stubbed supply the decline must pass through untouched"
        )
        assert not permitted.may_hand_off, "a closing reader cannot lend its FD"
        assert permitted.decline_reason is RustPumpDeclineReason.READER_PAUSE_FAILED, (
            "the unstubbed decline must keep its pause-failure reason"
        )

    def test_closing_pause_is_permitted_for_a_test_owned_extractor(self) -> None:
        """The seam lends its own descriptors, so the closing veto cannot apply."""
        configure_pump_stream_dispatch_for_testing(
            raw_fd_extractor=lambda _stream: 7,
        )

        permitted = _pipeline_streams._permit_test_owned_descriptor_handoff(
            self._closing_pause()
        )

        assert permitted.may_hand_off, "a stubbed supply must permit the hand-off"
        assert permitted.decline_reason is None, (
            "a permitted hand-off must drop the decline reason"
        )
        assert permitted.resume is None, (
            "nothing was paused, so the permitted verdict carries no resume"
        )
