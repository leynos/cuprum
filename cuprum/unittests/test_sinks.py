"""Tests for tee-path sink implementations."""

from __future__ import annotations

import errno
import os
import typing as typ

import pytest

from benchmarks import sinks


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_pty_blackhole_enter_cleans_up_when_fdopen_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PTY sink setup closes open file descriptors if fdopen fails."""
    master_fd, slave_fd = os.openpty()
    monkeypatch.setattr(sinks.pty, "openpty", lambda: (master_fd, slave_fd))

    def fail_fdopen(*_args: object, **_kwargs: object) -> typ.NoReturn:
        """Raise RuntimeError to simulate os.fdopen failing."""
        msg = "fdopen failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(sinks.os, "fdopen", fail_fdopen)
    blackhole = sinks.PtyBlackhole(encoding="utf-8", errors="replace")

    with pytest.raises(RuntimeError, match="fdopen failed"):
        blackhole.__enter__()

    def fstat_error(fd: int) -> OSError:
        """Return the OSError raised when fstat is called on a closed fd."""
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


# ---------------------------------------------------------------------------
# TextBlackhole
# ---------------------------------------------------------------------------


def test_text_blackhole_is_writable() -> None:
    """TextBlackhole reports itself as writable."""
    bh = sinks.TextBlackhole()
    assert bh.writable() is True


def test_text_blackhole_write_returns_char_count() -> None:
    """TextBlackhole.write returns the length of the string written."""
    bh = sinks.TextBlackhole()
    assert bh.write("hello") == 5
    assert bh.write("") == 0
    assert bh.write("x" * 1000) == 1000


def test_text_blackhole_flush_is_a_noop() -> None:
    """TextBlackhole.flush completes without raising."""
    bh = sinks.TextBlackhole()
    bh.flush()  # must not raise


def test_text_blackhole_write_rejects_non_str() -> None:
    """TextBlackhole.write raises TypeError for non-str input."""
    bh = sinks.TextBlackhole()
    with pytest.raises(TypeError):
        bh.write(b"bytes")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PtyBlackhole happy path
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_pty_blackhole_enter_returns_writable_stream() -> None:
    """PtyBlackhole.__enter__ returns a writable text IO stream."""
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    with bh as stream:
        assert stream.writable()


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_pty_blackhole_drains_written_bytes() -> None:
    """Data written to the PtyBlackhole slave FD is consumed by the drainer."""
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    with bh as stream:
        stream.write("hello from test\n")
        stream.flush()
    # If __exit__ completes without hanging the drainer consumed the data.


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_pty_blackhole_exit_clears_internal_state() -> None:
    """PtyBlackhole.__exit__ clears _master_fd, _slave, and _thread."""
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    with bh:
        pass
    assert bh._master_fd is None
    assert bh._slave is None
    assert bh._thread is None


# ---------------------------------------------------------------------------
# open_sink factory
# ---------------------------------------------------------------------------


def test_open_sink_devnull_yields_writable_stream() -> None:
    """open_sink('devnull') yields a writable text stream."""
    with sinks.open_sink("devnull", encoding="utf-8", errors="replace") as stream:
        assert stream.writable()
        n = stream.write("test")
        assert n > 0


def test_open_sink_text_blackhole_yields_text_blackhole() -> None:
    """open_sink('text_blackhole') yields a TextBlackhole instance."""
    with sinks.open_sink(
        "text_blackhole",
        encoding="utf-8",
        errors="replace",
    ) as stream:
        assert isinstance(stream, sinks.TextBlackhole)
        assert stream.write("hello") == 5


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_open_sink_pty_blackhole_yields_writable_stream() -> None:
    """open_sink('pty_blackhole') yields a writable text stream."""
    with sinks.open_sink("pty_blackhole", encoding="utf-8", errors="replace") as stream:
        assert stream.writable()
