"""Lifecycle coverage for native workers that outlive their event loop."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import os
import threading
import time
import typing as typ

import pytest

from cuprum import (
    _pipeline_stream_fds,
    _pipeline_stream_native_cleanup,
    _pipeline_streams,
)
from cuprum.unittests._rust_pump_test_helpers import install_fake_pump

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _RecordingGuard:
    """Blocking-mode guard double that signals fallback completion."""

    def __init__(self, restored: threading.Event) -> None:
        """Retain the completion signal for the loop-closure assertion."""
        self.restored = restored

    def restore(self) -> None:
        """Record that fallback cleanup restored descriptor state."""
        self.restored.set()


@dc.dataclass(slots=True)
class _ExecutorLifecycle:
    """Synchronization and descriptor evidence for one late worker."""

    worker_started: threading.Event = dc.field(default_factory=threading.Event)
    release_worker: threading.Event = dc.field(default_factory=threading.Event)
    restored: threading.Event = dc.field(default_factory=threading.Event)
    native_fds: list[tuple[int, int]] = dc.field(default_factory=list)


def test_sync_cancellation_returns_before_late_native_worker_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed caller loop cannot prevent late descriptor finalization."""
    lifecycle = _ExecutorLifecycle()

    def blocking_pump(reader_fd: int, writer_fd: int) -> int:
        """Model Rust borrowing the reader and owning the submitted writer."""
        lifecycle.native_fds.append((reader_fd, writer_fd))
        lifecycle.worker_started.set()
        if not lifecycle.release_worker.wait(timeout=5.0):
            lifecycle.release_worker.wait()
        os.close(writer_fd)
        return 0

    _install_blocking_pump(monkeypatch, blocking_pump)
    reader_fd, writer_fd = os.pipe()
    state = _pipeline_stream_native_cleanup._RustPumpState(
        reader_fd=reader_fd,
        writer_fd=writer_fd,
        blocking_mode_guard=typ.cast(
            "_pipeline_stream_fds._BlockingModeGuard",
            _RecordingGuard(lifecycle.restored),
        ),
        resume_reader=None,
        cleanup_grace_s=0.01,
    )

    async def cancel_native_pump() -> None:
        """Cancel one native pump after its worker has begun blocking."""
        task = asyncio.create_task(
            _pipeline_streams._run_rust_pump_with_blocking_fds(state=state)
        )
        started = await asyncio.to_thread(lifecycle.worker_started.wait, 5.0)
        assert started, "the native worker must begin before cancellation"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    started_at = time.monotonic()
    asyncio.run(cancel_native_pump())
    elapsed_s = time.monotonic() - started_at
    assert elapsed_s < 0.5, (
        "the synchronous caller must return at cleanup grace rather than wait "
        f"for native I/O; elapsed {elapsed_s:.6f}s"
    )
    assert len(lifecycle.native_fds) == 1, (
        "the worker must retain both native duplicates"
    )
    native_reader_fd, native_writer_fd = lifecycle.native_fds[0]
    os.fstat(native_reader_fd)
    os.fstat(native_writer_fd)

    lifecycle.release_worker.set()
    assert lifecycle.restored.wait(timeout=5.0), (
        "late completion must finalize descriptor state after the caller loop closes"
    )
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(native_reader_fd)
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(native_writer_fd)
    assert _wait_for_deferred_cleanup(), (
        "late completion must release the retained worker future"
    )


def _install_blocking_pump(
    monkeypatch: pytest.MonkeyPatch,
    pump: cabc.Callable[[int, int], int],
) -> None:
    """Install the native worker double without leaking module setup details."""
    install_fake_pump(monkeypatch, pump)


def _wait_for_deferred_cleanup() -> bool:
    """Wait for the completion callback to discard its retained worker future."""
    deadline = time.monotonic() + 5.0
    while _pipeline_stream_native_cleanup._NATIVE_PUMP_FUTURES:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True
