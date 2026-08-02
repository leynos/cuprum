"""Shared fixtures and helpers for pump-stream dispatch selection tests.

Split from the oversized ``test_pipeline_stream_backend_selection`` module so
the non-blocking pipe context manager, pause-free transport stubs, backend
cache-clearing fixture, and Rust/Python pump doubles have a single, focused
home shared by the selection and FD-blocking test modules.
"""

from __future__ import annotations

import asyncio
import contextlib
import typing as typ

import pytest

from cuprum import _pipeline_streams
from cuprum._testing import (
    reset_pump_stream_dispatch_for_testing,
    set_rust_availability_for_testing,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_WRITER_TOGGLE_FAILURE = "writer toggle failed"


class PumpCallCounts(typ.TypedDict, total=False):
    """Count Rust and Python pump dispatches in test doubles."""

    rust_pump: int
    python_pump: int


@contextlib.contextmanager
def _nonblocking_pipe_pair() -> cabc.Iterator[tuple[int, int, int, int]]:
    """Yield two pipes with active ends configured for non-blocking I/O."""
    with contextlib.ExitStack() as stack:
        # Register each descriptor as soon as it exists: a failure part-way
        # through setup — the second ``os.pipe`` or either ``set_blocking`` —
        # must still close everything already acquired.
        read_fd, read_write_fd = _pipeline_streams.os.pipe()
        stack.callback(_pipeline_streams.os.close, read_fd)
        stack.callback(_pipeline_streams.os.close, read_write_fd)
        write_read_fd, write_fd = _pipeline_streams.os.pipe()
        stack.callback(_pipeline_streams.os.close, write_read_fd)
        stack.callback(_pipeline_streams.os.close, write_fd)
        _pipeline_streams.os.set_blocking(read_fd, False)
        _pipeline_streams.os.set_blocking(write_fd, False)
        yield read_fd, read_write_fd, write_read_fd, write_fd


class _TransportWithoutPause:
    """Transport shim exposing fileno() and pipe info for tests."""

    def __init__(self, fd: int) -> None:
        """Store the file descriptor backing this stub transport."""
        self._fd = fd

    def get_extra_info(self, name: str) -> object | None:
        """Return stub transport info for the requested key."""
        if name != "pipe":
            return None
        return self

    def fileno(self) -> int:
        """Return the file descriptor exposed by this stub transport."""
        return self._fd


class _ReaderWithoutPause:
    """Test reader object holding a transport without pause support."""

    def __init__(self, fd: int) -> None:
        """Attach a pause-free transport built from the given descriptor."""
        self.transport = _TransportWithoutPause(fd)


class _WriterWithoutPause:
    """Test writer object holding a transport without pause support."""

    def __init__(self, fd: int) -> None:
        """Attach a pause-free transport built from the given descriptor."""
        self.transport = _TransportWithoutPause(fd)


@pytest.fixture
def clear_backend_caches() -> cabc.Iterator[None]:
    """Clear and restore backend-selection test hooks for each test."""
    from cuprum import _backend

    def reset() -> None:
        """Reset availability, dispatch, and both backend caches."""
        set_rust_availability_for_testing(is_available=None)
        reset_pump_stream_dispatch_for_testing()
        _backend._check_rust_available.cache_clear()
        _backend.get_stream_backend.cache_clear()

    reset()
    yield
    reset()


def _make_blocking_fd_spy(
    calls: PumpCallCounts,
    expected_reader_fd: int,
    expected_writer_fd: int,
) -> cabc.Callable[[int, int], int]:
    """Return a fake ``rust_pump_stream`` that asserts FDs are blocking."""

    def _spy(reader_fd: int, writer_fd: int) -> int:
        """Assert both descriptors are blocking, then record the call."""
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


async def _fake_python_fallback(
    reader: asyncio.StreamReader | None,
    writer: asyncio.StreamWriter | None,
    calls: PumpCallCounts,
) -> None:
    """Stand in for the Python pump fallback and record that it ran."""
    del reader, writer
    await asyncio.sleep(0)
    calls["python_pump"] += 1
