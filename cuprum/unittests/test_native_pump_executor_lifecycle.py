"""Lifecycle coverage for native workers that outlive their event loop."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import os
import threading
import time
import typing as typ
from unittest import mock

import pytest

from cuprum import (
    ExecutionContext,
    ScopeConfig,
    TimeoutExpired,
    _pipeline_stream_fds,
    _pipeline_stream_native_cleanup,
    _pipeline_streams,
    scoped,
    sh,
)
from cuprum._backend import (
    _check_rust_available,
    get_stream_backend,
    set_rust_availability_for_testing,
)
from cuprum._testing import (
    configure_pump_stream_dispatch_for_testing,
    reset_pump_stream_dispatch_for_testing,
)
from cuprum.unittests._rust_pump_test_helpers import install_fake_pump
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.program import Program
    from cuprum.sh import Pipeline


_SYNC_PIPELINE_TIMEOUT_S = 0.05
_SYNC_PIPELINE_CLEANUP_GRACE_S = 0.01
_SYNC_PIPELINE_RETURN_BOUND_S = (
    _SYNC_PIPELINE_TIMEOUT_S + _SYNC_PIPELINE_CLEANUP_GRACE_S + 0.75
)


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
    worker_finished: threading.Event = dc.field(default_factory=threading.Event)
    restored: threading.Event = dc.field(default_factory=threading.Event)
    native_fds: list[tuple[int, int]] = dc.field(default_factory=list)


@dc.dataclass(slots=True)
class _SyncPipelineRun:
    """Capture one synchronous pipeline result from its caller thread."""

    completed: threading.Event = dc.field(default_factory=threading.Event)
    errors: list[BaseException] = dc.field(default_factory=list)
    rust_attempts: list[None] = dc.field(default_factory=list)


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


def _make_sync_timeout_pipeline() -> tuple[Pipeline, frozenset[Program]]:
    """Build a two-stage pipeline that keeps its final stage alive for timeout."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    pipeline = python("-c", "import sys; sys.stdout.write('payload')") | python(
        "-c", "import time; time.sleep(60)"
    )
    return pipeline, frozenset((python_program,))


def _install_sync_pipeline_worker(
    monkeypatch: pytest.MonkeyPatch,
    lifecycle: _ExecutorLifecycle,
) -> tuple[mock.Mock, mock.Mock]:
    """Install a held worker and record the completion callback's cleanup."""
    native_reader_close = mock.Mock(
        wraps=_pipeline_stream_native_cleanup._close_rust_reader_fd
    )
    state_fd_close = mock.Mock(
        wraps=_pipeline_stream_native_cleanup._close_rust_state_fd
    )
    original_restore = _pipeline_stream_fds._BlockingModeGuard.restore

    def blocking_pump(reader_fd: int, writer_fd: int) -> int:
        """Hold the native duplicates until the test releases Rust ownership."""
        lifecycle.native_fds.append((reader_fd, writer_fd))
        lifecycle.worker_started.set()
        lifecycle.release_worker.wait()
        os.close(writer_fd)
        lifecycle.worker_finished.set()
        return 0

    def record_restore(guard: _pipeline_stream_fds._BlockingModeGuard) -> None:
        """Require callback state restoration after worker-owned I/O ends."""
        assert lifecycle.worker_finished.is_set(), (
            "the completion callback must not restore state before native I/O ends"
        )
        original_restore(guard)
        lifecycle.restored.set()

    monkeypatch.setattr(
        _pipeline_stream_native_cleanup,
        "_close_rust_reader_fd",
        native_reader_close,
    )
    monkeypatch.setattr(
        _pipeline_stream_native_cleanup,
        "_close_rust_state_fd",
        state_fd_close,
    )
    monkeypatch.setattr(
        _pipeline_stream_fds._BlockingModeGuard, "restore", record_restore
    )
    _install_blocking_pump(monkeypatch, blocking_pump)
    return native_reader_close, state_fd_close


def _enable_rust_raw_fd_path(sync_run: _SyncPipelineRun) -> None:
    """Configure and clear the test seam for the real stream descriptors."""
    configure_pump_stream_dispatch_for_testing(
        on_rust_fd_path_attempt=lambda: sync_run.rust_attempts.append(None),
    )
    set_rust_availability_for_testing(is_available=True)
    _check_rust_available.cache_clear()
    get_stream_backend.cache_clear()


def _reset_rust_raw_fd_path() -> None:
    """Restore backend selection after a synchronous lifecycle regression test."""
    reset_pump_stream_dispatch_for_testing()
    set_rust_availability_for_testing(is_available=None)
    _check_rust_available.cache_clear()
    get_stream_backend.cache_clear()


def _run_sync_timeout_pipeline(
    pipeline: Pipeline,
    allowlist: frozenset[Program],
    sync_run: _SyncPipelineRun,
) -> None:
    """Invoke the public synchronous pipeline API and retain its exception."""
    try:
        with scoped(ScopeConfig(allowlist=allowlist)):
            pipeline.run_sync(
                timeout=_SYNC_PIPELINE_TIMEOUT_S,
                context=ExecutionContext(
                    native_pump_cleanup_grace=_SYNC_PIPELINE_CLEANUP_GRACE_S
                ),
            )
    except TimeoutExpired as error:
        sync_run.errors.append(error)
    finally:
        sync_run.completed.set()


def _assert_sync_timeout_returned(
    caller: threading.Thread,
    sync_run: _SyncPipelineRun,
    started_at: float,
) -> None:
    """Assert the public caller returns before its held worker is released."""
    caller.join(timeout=_SYNC_PIPELINE_RETURN_BOUND_S)
    elapsed_s = time.monotonic() - started_at
    assert not caller.is_alive(), (
        "run_sync must return after timeout plus cleanup grace rather than "
        f"wait for native I/O; elapsed {elapsed_s:.6f}s"
    )
    assert sync_run.completed.is_set(), "run_sync must publish its outcome"
    assert len(sync_run.errors) == 1, (
        f"run_sync must raise one public error, found {sync_run.errors!r}"
    )
    assert isinstance(sync_run.errors[0], TimeoutExpired), (
        f"run_sync must raise TimeoutExpired, found {sync_run.errors!r}"
    )


def _assert_late_pipeline_completion(
    lifecycle: _ExecutorLifecycle,
    native_reader_close: mock.Mock,
    state_fd_close: mock.Mock,
) -> None:
    """Assert callback cleanup after the caller's event loop has closed."""
    native_reader_fd, native_writer_fd = lifecycle.native_fds[0]
    assert lifecycle.restored.wait(timeout=5.0), (
        "late completion must restore blocking mode after native I/O stops"
    )
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(native_reader_fd)
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(native_writer_fd)
    assert native_reader_close.call_args_list == [mock.call(native_reader_fd)], (
        "the callback must close its borrowed native reader exactly once"
    )
    assert state_fd_close.call_count == 2, (
        "the callback must close both callback-owned state descriptors"
    )
    assert _wait_for_deferred_cleanup(), (
        "late completion must release the retained worker future"
    )


def test_sync_pipeline_timeout_returns_before_late_native_worker_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_sync`` does not wait for a held native worker at loop shutdown."""
    lifecycle = _ExecutorLifecycle()
    sync_run = _SyncPipelineRun()
    pipeline, allowlist = _make_sync_timeout_pipeline()
    native_reader_close, state_fd_close = _install_sync_pipeline_worker(
        monkeypatch,
        lifecycle,
    )
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
    _enable_rust_raw_fd_path(sync_run)
    caller = threading.Thread(
        target=_run_sync_timeout_pipeline,
        args=(pipeline, allowlist, sync_run),
    )
    started_at = time.monotonic()
    caller.start()
    try:
        assert lifecycle.worker_started.wait(timeout=5.0), (
            "the two-stage pipeline must start the Rust raw-FD pump"
        )
        _assert_sync_timeout_returned(caller, sync_run, started_at)
        assert sync_run.rust_attempts == [None], (
            "the pipeline must take the Rust raw-FD path"
        )
        assert len(lifecycle.native_fds) == 1, (
            "the worker must retain both native descriptor duplicates"
        )
        native_reader_fd, native_writer_fd = lifecycle.native_fds[0]
        os.fstat(native_reader_fd)
        os.fstat(native_writer_fd)
        assert not lifecycle.restored.is_set(), (
            "descriptor restoration must remain deferred while Rust owns duplicates"
        )
    finally:
        lifecycle.release_worker.set()
        caller.join(timeout=5.0)
        _reset_rust_raw_fd_path()

    assert not caller.is_alive(), "releasing the worker must settle the caller thread"
    _assert_late_pipeline_completion(lifecycle, native_reader_close, state_fd_close)


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
