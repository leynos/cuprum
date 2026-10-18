"""Tests for pending PTY drains that finish after bounded shutdown."""

from __future__ import annotations

import os
import threading
from contextlib import ExitStack

import pytest

from benchmarks import PtyBlackholeDrainError, _pty_setup, pty_blackhole, sinks


class _DelayedPtyReader:
    """Hold the PTY reader until shutdown has recorded its timeout."""

    def __init__(self, blackhole: sinks.PtyBlackhole) -> None:
        """Retain the PTY and OS operations needed to gate the reader."""
        self.blackhole = blackhole
        self.master_fd: int | None = None
        self.reader_entered = threading.Event()
        self.release_reader = threading.Event()
        self._openpty = _pty_setup.pty.openpty
        self._read = os.read
        self._join = threading.Thread.join

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace PTY reads and the bounded drainer join with gated versions."""

        def return_before_reader_stops(
            thread: threading.Thread,
            timeout: float | None = None,
        ) -> None:
            """Simulate the bounded join expiring while the reader is gated."""
            if thread is self.blackhole._thread and timeout is not None:
                return
            self._join(thread, timeout)

        monkeypatch.setattr(_pty_setup.pty, "openpty", self.capture_openpty)
        monkeypatch.setattr(pty_blackhole.os, "read", self.gated_read)
        monkeypatch.setattr(threading.Thread, "join", return_before_reader_stops)

    def capture_openpty(self) -> tuple[int, int]:
        """Capture the master descriptor held by the delayed reader."""
        master_fd, slave_fd = self._openpty()
        self.master_fd = master_fd
        return master_fd, slave_fd

    def gated_read(self, fd: int, size: int) -> bytes:
        """Hold the selected master read until the test releases the reader."""
        if fd == self.master_fd:
            self.reader_entered.set()
            if not self.release_reader.wait(timeout=5.0):
                msg = "reader gate was not released"
                raise TimeoutError(msg)
        return self._read(fd, size)

    def release_and_join(self) -> None:
        """Release the delayed read and wait for its cleanup to finish."""
        self.release_reader.set()
        thread = self.blackhole._thread
        assert thread is not None
        self._join(thread, timeout=5.0)


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_timed_out_drain_keeps_resources_open_for_late_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the PTY open until a delayed reader acknowledges accepted bytes."""
    blackhole = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    delayed_reader = _DelayedPtyReader(blackhole)
    delayed_reader.install(monkeypatch)

    with ExitStack() as stack:
        stream = stack.enter_context(blackhole)
        slave_fd = stream.fileno()
        stream.write("late output")
        stream.flush()
        wake_write_fd = blackhole._wake_write_fd
        assert wake_write_fd is not None
        assert delayed_reader.reader_entered.wait(timeout=5.0), (
            "reader must reach its gated read"
        )

        try:
            with pytest.raises(PtyBlackholeDrainError, match="timed out"):
                stack.close()

            assert blackhole.drained_bytes is None
            os.fstat(slave_fd)
            os.fstat(wake_write_fd)
        finally:
            delayed_reader.release_and_join()

    assert blackhole.drained_bytes == len(b"late output")
    assert blackhole._slave is None, "late completion must release the slave"
    assert blackhole._wake_write_fd is None, "late completion must close the wake pipe"
    blackhole._publish_finished_drainer()
