"""Tests for transient readiness while draining PTY output."""

from __future__ import annotations

import errno
import os
import threading
import typing as typ

import pytest

from benchmarks import _pty_setup, pty_blackhole, sinks

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_pty_blackhole_retries_transient_master_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry a selected PTY read if the kernel no longer reports bytes."""
    blackhole = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    master_fds: list[int] = []
    original_openpty = _pty_setup.pty.openpty
    original_select = pty_blackhole.select.select
    original_read = os.read
    spurious_readiness = threading.Event()
    transient_read = threading.Event()

    def capture_openpty() -> tuple[int, int]:
        """Capture the master descriptor whose readiness is simulated."""
        master_fd, slave_fd = original_openpty()
        master_fds.append(master_fd)
        return master_fd, slave_fd

    def report_spurious_readiness(
        readers: cabc.Sequence[int],
        writers: cabc.Sequence[int],
        errors: cabc.Sequence[int],
    ) -> tuple[list[int], list[int], list[int]]:
        """Report one empty master read before delegating to real select."""
        master_fd = master_fds[0]
        if master_fd in readers and not spurious_readiness.is_set():
            spurious_readiness.set()
            return [master_fd], [], []
        return original_select(readers, writers, errors)

    def read_once_after_transient_ready(fd: int, size: int) -> bytes:
        """Make the first selected master read behave like nonblocking EAGAIN."""
        if fd == master_fds[0] and not transient_read.is_set():
            transient_read.set()
            raise BlockingIOError(errno.EAGAIN, "transient PTY readiness")
        return original_read(fd, size)

    monkeypatch.setattr(_pty_setup.pty, "openpty", capture_openpty)
    monkeypatch.setattr(
        pty_blackhole.select,
        "select",
        report_spurious_readiness,
    )
    monkeypatch.setattr(pty_blackhole.os, "read", read_once_after_transient_ready)

    with blackhole as stream:
        assert transient_read.wait(timeout=5.0), "the reader must retry EAGAIN"
        stream.write("payload")

    assert blackhole.drained_bytes == len(b"payload")
