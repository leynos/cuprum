"""Lifecycle coverage for native workers that outlive their event loop."""

from __future__ import annotations

import asyncio
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
    _pipeline_collect,
    _pipeline_stream_fds,
    _pipeline_stream_native_cleanup,
    _pipeline_streams,
    scoped,
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
from cuprum.unittests._native_pump_lifecycle_test_helpers import (
    _SYNC_PIPELINE_CLEANUP_GRACE_S,
    _SYNC_PIPELINE_TIMEOUT_S,
    _arm_timeout_after_worker_starts,
    _assert_sync_timeout_returned,
    _delay_pump_dispatch,
    _ExecutorLifecycle,
    _make_sync_timeout_pipeline,
    _RecordingGuard,
    _SyncPipelineRun,
    _wait_for_deferred_cleanup,
)
from cuprum.unittests._rust_pump_test_helpers import install_fake_pump

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.program import Program
    from cuprum.sh import Pipeline


def test_sync_cancellation_returns_before_late_native_worker_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed caller loop cannot prevent late descriptor finalization."""
    lifecycle = _ExecutorLifecycle()

    def blocking_pump(reader_fd: int, writer_fd: int) -> int:
        """Model Rust borrowing the reader and owning the submitted writer."""
        lifecycle.native_fds.append((reader_fd, writer_fd))
        lifecycle.worker_started.set()
        deadline = time.monotonic() + 5.0
        if not lifecycle.release_worker.wait(
            timeout=max(0.0, deadline - time.monotonic())
        ):
            msg = "late native worker was not released before its deadline"
            raise TimeoutError(msg)
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
        deadline = time.monotonic() + 5.0
        if not lifecycle.release_worker.wait(
            timeout=max(0.0, deadline - time.monotonic())
        ):
            msg = "synchronous pipeline worker was not released before its deadline"
            raise TimeoutError(msg)
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
    assert _wait_for_deferred_cleanup(), (
        "late completion must release the retained worker future"
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


def _assert_held_native_descriptors_are_valid(
    lifecycle: _ExecutorLifecycle,
) -> None:
    """Confirm that the blocked worker still owns usable native duplicates."""
    assert len(lifecycle.native_fds) == 1, (
        "the worker must retain both native descriptor duplicates"
    )
    for native_fd in lifecycle.native_fds[0]:
        os.fstat(native_fd)


@pytest.mark.parametrize(
    "startup_delay_s", [0.0, 0.1], ids=["immediate-start", "delayed-start"]
)
def test_sync_pipeline_timeout_returns_before_late_native_worker_settles(
    monkeypatch: pytest.MonkeyPatch,
    startup_delay_s: float,
) -> None:
    """``run_sync`` does not wait for a held native worker at loop shutdown."""
    lifecycle = _ExecutorLifecycle()
    sync_run = _SyncPipelineRun()
    pipeline, allowlist = _make_sync_timeout_pipeline()
    native_reader_close, state_fd_close = _install_sync_pipeline_worker(
        monkeypatch,
        lifecycle,
    )
    create_pipe_tasks = mock.Mock(wraps=_pipeline_collect._create_pipe_tasks)
    monkeypatch.setattr(_pipeline_collect, "_create_pipe_tasks", create_pipe_tasks)
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
    _enable_rust_raw_fd_path(sync_run)
    _delay_pump_dispatch(monkeypatch, startup_delay_s)
    _arm_timeout_after_worker_starts(monkeypatch, lifecycle.worker_started)
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
        assert create_pipe_tasks.call_args is not None, (
            "the pipeline must create its inter-stage pump tasks"
        )
        assert (
            create_pipe_tasks.call_args.kwargs["native_pump_cleanup_grace"]
            == _SYNC_PIPELINE_CLEANUP_GRACE_S
        ), "the pipeline must pass its configured native cleanup grace to each hop"
        _assert_held_native_descriptors_are_valid(lifecycle)
        assert not lifecycle.restored.is_set(), (
            "descriptor restoration must remain deferred while Rust owns duplicates"
        )
    finally:
        lifecycle.release_worker.set()
        caller.join(timeout=5.0)
        _reset_rust_raw_fd_path()

    assert not caller.is_alive(), "releasing the worker must settle the caller thread"
    _assert_late_pipeline_completion(lifecycle, native_reader_close, state_fd_close)


@pytest.mark.parametrize(
    "startup_delay_s", [0.0, 0.1], ids=["immediate-start", "delayed-start"]
)
def test_deferred_grace_releases_paused_reader_before_loop_closes(
    monkeypatch: pytest.MonkeyPatch,
    startup_delay_s: float,
) -> None:
    """A hop deferred past its grace releases its reader transport, not just resumes it.

    Deferral hands the reader to a callback that outlives the caller's loop, so
    that loop can never resume the transport. A paused transport that is never
    resumed or closed keeps its descriptor, keeps its subprocess transport open,
    and reports itself once collected — after the loop is gone, which is why the
    assertion below reads resource ownership rather than warning capture.
    """
    lifecycle = _ExecutorLifecycle()
    sync_run = _SyncPipelineRun()
    pipeline, allowlist = _make_sync_timeout_pipeline()
    _install_sync_pipeline_worker(monkeypatch, lifecycle)
    create_pipe_tasks = mock.Mock(wraps=_pipeline_collect._create_pipe_tasks)
    monkeypatch.setattr(_pipeline_collect, "_create_pipe_tasks", create_pipe_tasks)
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
    _enable_rust_raw_fd_path(sync_run)
    _delay_pump_dispatch(monkeypatch, startup_delay_s)
    _arm_timeout_after_worker_starts(monkeypatch, lifecycle.worker_started)
    caller = threading.Thread(
        target=_run_sync_timeout_pipeline,
        args=(pipeline, allowlist, sync_run),
    )
    caller.start()
    try:
        assert lifecycle.worker_started.wait(timeout=5.0), (
            "the two-stage pipeline must start the Rust raw-FD pump"
        )
        _assert_sync_timeout_returned(caller, sync_run, time.monotonic())
        _assert_held_native_descriptors_are_valid(lifecycle)
        _assert_caller_reader_transport_released(create_pipe_tasks)
    finally:
        lifecycle.release_worker.set()
        caller.join(timeout=5.0)
        _reset_rust_raw_fd_path()
    assert not caller.is_alive(), "releasing the worker must settle the caller thread"


def _assert_caller_reader_transport_released(
    create_pipe_tasks: mock.Mock,
) -> None:
    """Assert the caller's loop left no unresolved reader transport behind."""
    assert create_pipe_tasks.call_args is not None, (
        "the pipeline must create its inter-stage pump tasks"
    )
    processes = create_pipe_tasks.call_args.args[0]
    upstream = typ.cast(
        "asyncio.subprocess.Process",
        processes[0],
    )
    transport = typ.cast(
        "asyncio.SubprocessTransport",
        getattr(upstream, "_transport", None),
    )
    assert transport is not None, (
        "the upstream stage must keep its subprocess transport"
    )
    reader = transport.get_pipe_transport(1)
    assert reader is not None, "the upstream stage must keep its piped stdout"
    assert reader.is_closing(), (
        "a reader deferred past its cleanup grace must be released before its "
        "loop closes; a merely paused transport would keep its descriptor and "
        "report itself once the loop is gone"
    )
    assert transport.is_closing(), (
        "releasing the paused reader must let the subprocess transport close"
    )
    assert transport.get_returncode() is not None, (
        "releasing the paused reader must let the subprocess transport finish"
    )


def _install_blocking_pump(
    monkeypatch: pytest.MonkeyPatch,
    pump: cabc.Callable[[int, int], int],
) -> None:
    """Install the native worker double without leaking module setup details."""
    install_fake_pump(monkeypatch, pump)
