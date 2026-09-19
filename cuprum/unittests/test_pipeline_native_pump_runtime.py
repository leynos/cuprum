"""Regression coverage for native-pump executor lifetime semantics."""

from __future__ import annotations

import subprocess  # ruff: ignore[suspicious-subprocess-import] - this test isolates interpreter shutdown.
import sys
import textwrap
import threading
import time

from cuprum._pipeline_native_pump_runtime import _PooledNativePumpExecutor


def test_submission_never_waits_behind_a_busy_worker() -> None:
    """Every submission starts at once, however many workers are busy.

    A native pump cannot finish while its downstream pipe is full, and only a
    later hop in the same pipeline can drain that pipe, so a submission that
    waited for a free worker could wait forever. The idle retention limit must
    therefore cap reuse only, never concurrency.
    """
    executor = _PooledNativePumpExecutor(idle_limit=2)
    started: list[int] = []
    started_lock = threading.Lock()
    release = threading.Event()
    submitted = 8

    def block(reader_fd: int, _writer_fd: int) -> int:
        """Occupy a worker until the test releases every blocked pump."""
        with started_lock:
            started.append(reader_fd)
        release.wait()
        return 0

    futures = [executor.submit(block, token, token) for token in range(submitted)]
    deadline = time.monotonic() + 5.0
    try:
        while time.monotonic() < deadline:
            with started_lock:
                if len(started) == submitted:
                    break
            time.sleep(0.01)
        with started_lock:
            started_count = len(started)
    finally:
        release.set()

    assert started_count == submitted, (
        f"every submission must begin without waiting for a free worker, "
        f"but only {started_count} of {submitted} started while all workers "
        f"were busy"
    )
    assert [future.result(timeout=5.0) for future in futures] == [0] * submitted


def test_idle_workers_are_reused_within_the_retention_limit() -> None:
    """Sequential hand-offs recycle idle workers instead of starting new ones."""
    executor = _PooledNativePumpExecutor(idle_limit=2)
    baseline = threading.active_count()

    def quick(_reader_fd: int, _writer_fd: int) -> int:
        """Complete immediately so the worker returns to the idle pool."""
        return 0

    for _ in range(12):
        executor.submit(quick, 0, 0).result(timeout=5.0)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and len(executor._idle) < 2:
        time.sleep(0.01)

    assert len(executor._idle) <= 2, (
        f"idle retention must stay within its limit, found {len(executor._idle)}"
    )
    assert threading.active_count() <= baseline + 2, (
        f"repeated hand-offs must reuse workers rather than start a thread "
        f"each time, found {threading.active_count()} threads against a "
        f"baseline of {baseline}"
    )


def test_pooled_native_pump_executor_does_not_join_at_exit() -> None:
    """A blocked native worker cannot prevent interpreter shutdown."""
    script = textwrap.dedent(
        """
        import threading

        from cuprum._pipeline_native_pump_runtime import _PooledNativePumpExecutor

        started = threading.Event()
        release = threading.Event()

        def block(_reader_fd: int, _writer_fd: int) -> int:
            started.set()
            release.wait()
            return 0

        _PooledNativePumpExecutor().submit(block, 0, 0)
        if not started.wait(timeout=1.0):
            raise RuntimeError("native worker did not start")
        """
    )
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed interpreter invocation.
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        text=True,
        timeout=5.0,
    )

    assert completed.returncode == 0, completed.stderr
