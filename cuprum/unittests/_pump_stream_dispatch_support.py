"""Shared fixtures and helpers for pump-stream dispatch selection tests.

Split from the oversized ``test_pipeline_stream_backend_selection`` module so
the non-blocking pipe context manager, pause-free transport stubs, backend
cache-clearing fixture, and Rust/Python pump doubles have a single, focused
home shared by the selection and FD-blocking test modules.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import typing as typ
from unittest import mock

import pytest

from cuprum import _pipeline_stream_fds, _pipeline_streams
from cuprum._testing import (
    reset_pump_stream_dispatch_for_testing,
    set_rust_availability_for_testing,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_WRITER_TOGGLE_FAILURE = "writer toggle failed"


class PumpCallCounts(typ.TypedDict, total=False):
    """Count Rust and Python pump dispatches in test doubles.

    Attributes
    ----------
    rust_pump : int
        Number of times the Rust pump was dispatched.
    python_pump : int
        Number of times the Python pump fallback was dispatched.
    """

    rust_pump: int
    python_pump: int


@contextlib.contextmanager
def _nonblocking_pipe_pair() -> cabc.Iterator[tuple[int, int, int, int]]:
    """Yield two pipes with active ends configured for non-blocking I/O."""
    with contextlib.ExitStack() as stack:
        # Register each descriptor as soon as it exists: a failure part-way
        # through setup — the second ``os.pipe`` or either ``set_blocking`` —
        # must still close everything already acquired.
        read_fd, read_write_fd = os.pipe()
        stack.callback(os.close, read_fd)
        stack.callback(os.close, read_write_fd)
        write_read_fd, write_fd = os.pipe()
        stack.callback(os.close, write_read_fd)
        stack.callback(os.close, write_fd)
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)
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
    """Clear and restore backend-selection caches and test hooks.

    Resets the Rust availability override, the pump-dispatch test hooks, and
    the ``_check_rust_available``/``get_stream_backend`` LRU caches before the
    test runs, then restores the prior state afterwards so backend selection
    for other tests is unaffected.

    Yields
    ------
    None
        Control passes to the test while the caches and hooks are cleared.
    """
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
        assert os.get_blocking(reader_fd), (
            "expected reader FD to be switched to blocking mode"
        )
        assert os.get_blocking(writer_fd), (
            "expected writer FD to be switched to blocking mode"
        )
        assert reader_fd != expected_reader_fd, (
            "expected Rust path to receive a duplicate, not the transport FD"
        )
        assert os.fstat(reader_fd).st_ino == os.fstat(expected_reader_fd).st_ino, (
            "expected the duplicate to refer to the same pipe as the reader FD"
        )
        # The native pump consumes its writer descriptor, so it must be handed
        # a duplicate rather than the descriptor asyncio's transport owns.
        assert writer_fd != expected_writer_fd, (
            "expected Rust path to receive a duplicate, not the transport FD"
        )
        assert os.fstat(writer_fd).st_ino == os.fstat(expected_writer_fd).st_ino, (
            "expected the duplicate to refer to the same pipe as the writer FD"
        )
        calls["rust_pump"] += 1
        # Model Rust's ownership: the duplicate is closed by the native pump.
        os.close(writer_fd)
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


async def _run_with_inline_executor_returning(
    awaitable: cabc.Awaitable[object],
) -> object:
    """Run mocked native work inline and return the awaited result.

    Submitted callables run immediately on the loop rather than in a thread,
    so a test double never spawns an unrelated thread pool.

    Parameters
    ----------
    awaitable : cabc.Awaitable[object]
        The coroutine to run with the executor patched.

    Returns
    -------
    object
        Whatever the awaited coroutine produced.
    """
    loop = asyncio.get_running_loop()

    def run_inline(
        executor: object,
        function: cabc.Callable[..., object],
        *args: object,
    ) -> asyncio.Future[object]:
        """Execute a submitted test double and publish its result immediately."""
        del executor
        future = loop.create_future()
        future.set_result(function(*args))
        return future

    with mock.patch.object(loop, "run_in_executor", side_effect=run_inline):
        return await awaitable


async def _run_with_inline_executor(awaitable: cabc.Awaitable[object]) -> None:
    """Run mocked native work without creating an unrelated thread pool.

    Parameters
    ----------
    awaitable : cabc.Awaitable[object]
        The coroutine to run with the executor patched.
    """
    await _run_with_inline_executor_returning(awaitable)


def install_closing_rust_pump(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Patch the native pump to consume the writer FD, as Rust does.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to replace ``rust_pump_stream``.

    Returns
    -------
    dict[str, int]
        Populated with ``writer_fd``, the descriptor the pump received.
    """
    received: dict[str, int] = {}

    def closing_rust_pump(reader_fd: int, writer_fd: int) -> int:
        """Record the writer descriptor and close it, mirroring ``OwnedFd``."""
        del reader_fd
        received["writer_fd"] = writer_fd
        os.close(writer_fd)
        return 0

    import cuprum._streams_rs as streams_rs

    monkeypatch.setattr(streams_rs, "rust_pump_stream", closing_rust_pump)
    return received


def bypass_reader_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the pause/drain step so a test can isolate FD ownership.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to replace the pause and drain helpers.
    """
    monkeypatch.setattr(
        _pipeline_streams,
        "_pause_reader_transport",
        lambda _reader: _pipeline_stream_fds._ReaderPause(may_hand_off=True),
    )

    async def _no_drain(
        reader: asyncio.StreamReader | None,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        """Stand in for the drain step without consuming anything."""
        del reader, writer
        await asyncio.sleep(0)

    monkeypatch.setattr(_pipeline_streams, "_drain_reader_buffer", _no_drain)
