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
_WRITER_TOGGLE_FAILURE = "writer toggle failed"


class _TransportWithoutPause:
    """Transport shim exposing fileno() and pipe info for tests."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def get_extra_info(self, name: str) -> object | None:
        if name != "pipe":
            return None
        return self

    def fileno(self) -> int:
        return self._fd


class _ReaderWithoutPause:
    """Test reader object holding a transport without pause support."""

    def __init__(self, fd: int) -> None:
        self.transport = _TransportWithoutPause(fd)


class _WriterWithoutPause:
    """Test writer object holding a transport without pause support."""

    def __init__(self, fd: int) -> None:
        self.transport = _TransportWithoutPause(fd)


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
    ) -> typ.Callable[[int, int], int]:
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

    @staticmethod
    async def _fake_python_fallback(
        reader: asyncio.StreamReader | None,
        writer: asyncio.StreamWriter | None,
        calls: dict[str, int],
    ) -> None:
        del reader, writer
        await asyncio.sleep(0)
        calls["python_pump"] += 1

    def test_run_rust_pump_pauses_reader_before_draining(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rust pumping should pause the reader transport before draining."""
        _ = self
        call_order: list[str] = []

        def fake_pause_reader_transport(
            reader: asyncio.StreamReader,
        ) -> typ.Callable[[], None]:
            del reader
            call_order.append("pause")

            def _resume() -> None:
                call_order.append("resume")

            return _resume

        async def fake_drain_reader_buffer(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter | None,
        ) -> None:
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

        monkeypatch.setattr(streams_rs, "rust_pump_stream", lambda *_: 0)

        reader = typ.cast("asyncio.StreamReader", object())
        asyncio.run(
            _pipeline_streams._run_rust_pump(
                reader=reader,
                writer=None,
                reader_fd=1,
                writer_fd=2,
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

        read_fd, read_write_fd, write_read_fd, write_fd = (
            self._create_nonblocking_pipe_pair()
        )
        try:
            monkeypatch.setattr(
                _pipeline_streams, "_extract_reader_fd", lambda _: read_fd
            )
            monkeypatch.setattr(
                _pipeline_streams, "_extract_writer_fd", lambda _: write_fd
            )

            original_set_blocking = _pipeline_streams.os.set_blocking
            calls = {"python_pump": 0}

            def fake_set_blocking(fd: int, blocking: object) -> None:
                if fd == read_fd and blocking is True:
                    desired_blocking = True
                    original_set_blocking(fd, desired_blocking)
                    return
                if fd == write_fd and blocking is True:
                    raise OSError(_WRITER_TOGGLE_FAILURE)
                original_set_blocking(fd, bool(blocking))

            monkeypatch.setattr(_pipeline_streams.os, "set_blocking", fake_set_blocking)
            configure_pump_stream_dispatch_for_testing(
                python_pump=lambda reader, writer: self._fake_python_fallback(
                    reader,
                    writer,
                    calls,
                )
            )

            reader = typ.cast("asyncio.StreamReader", object())
            writer = typ.cast("asyncio.StreamWriter", object())
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
        finally:
            _pipeline_streams.os.close(read_fd)
            _pipeline_streams.os.close(read_write_fd)
            _pipeline_streams.os.close(write_read_fd)
            _pipeline_streams.os.close(write_fd)

    def test_dispatch_uses_rust_when_reader_transport_cannot_pause(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing pause/resume hooks should not force a Python fallback."""
        _ = self
        monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
        set_rust_availability_for_testing(is_available=True)

        read_fd, read_write_fd, write_read_fd, write_fd = (
            self._create_nonblocking_pipe_pair()
        )
        try:
            calls = {"rust_pump": 0, "python_pump": 0}

            def fake_rust_pump_stream(reader_fd: int, writer_fd: int) -> int:
                assert reader_fd == read_fd
                assert writer_fd == write_fd
                calls["rust_pump"] += 1
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
                python_pump=lambda reader, writer: self._fake_python_fallback(
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
            asyncio.run(_pipeline_streams._pump_stream_dispatch(reader, writer))

            assert calls["rust_pump"] == 1, (
                "expected Rust path even when reader transport lacks pause hooks"
            )
            assert calls["python_pump"] == 0, (
                "did not expect Python fallback when Rust pump succeeds"
            )
        finally:
            _pipeline_streams.os.close(read_fd)
            _pipeline_streams.os.close(read_write_fd)
            _pipeline_streams.os.close(write_read_fd)
            _pipeline_streams.os.close(write_fd)
