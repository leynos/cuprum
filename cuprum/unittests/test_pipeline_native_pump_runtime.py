"""Regression coverage for native-pump executor lifetime semantics."""

from __future__ import annotations

import subprocess  # ruff: ignore[suspicious-subprocess-import] - this test isolates interpreter shutdown.
import sys
import textwrap


def test_persistent_native_pump_executor_does_not_join_at_exit() -> None:
    """A blocked native worker cannot prevent interpreter shutdown."""
    script = textwrap.dedent(
        """
        import threading

        from cuprum._pipeline_native_pump_runtime import _PersistentNativePumpExecutor

        started = threading.Event()
        release = threading.Event()

        def block(_reader_fd: int, _writer_fd: int) -> int:
            started.set()
            release.wait()
            return 0

        _PersistentNativePumpExecutor().submit(block, 0, 0)
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
