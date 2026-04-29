"""Tests for tee-path sink implementations."""

from __future__ import annotations

import errno
import os
import typing as typ

import pytest

from benchmarks import sinks


def test_pty_blackhole_enter_cleans_up_when_fdopen_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PTY sink setup closes open file descriptors if fdopen fails."""
    master_fd, slave_fd = os.openpty()
    monkeypatch.setattr(sinks.pty, "openpty", lambda: (master_fd, slave_fd))

    def fail_fdopen(*_args: object, **_kwargs: object) -> typ.NoReturn:
        msg = "fdopen failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(sinks.os, "fdopen", fail_fdopen)
    blackhole = sinks.PtyBlackhole(encoding="utf-8", errors="replace")

    with pytest.raises(RuntimeError, match="fdopen failed"):
        blackhole.__enter__()

    def fstat_error(fd: int) -> OSError:
        try:
            os.fstat(fd)
        except OSError as exc:
            return exc
        pytest.fail(f"expected closed fd {fd} to raise OSError")

    for fd in (master_fd, slave_fd):
        exc = fstat_error(fd)
        assert exc.errno == errno.EBADF, (
            f"expected EBADF for closed fd {fd}, got {exc.errno}"
        )
    assert blackhole._master_fd is None, (
        f"expected master fd state to be reset, got {blackhole._master_fd}"
    )
    assert blackhole._slave is None, (
        f"expected slave state to be reset, got {blackhole._slave}"
    )
    assert blackhole._thread is None, (
        f"expected thread state to be reset, got {blackhole._thread}"
    )
