"""Tests for tee-path sink implementations."""

from __future__ import annotations

import errno
import os
import threading
import typing as typ
from unittest import mock

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


def test_text_blackhole_write_rejects_non_str() -> None:
    """TextBlackhole.write raises TypeError for non-str input."""
    bh = sinks.TextBlackhole()
    # The cast documents the deliberately wrong-typed argument under test.
    with pytest.raises(TypeError):
        bh.write(typ.cast("str", b"bytes"))


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
    # Deliberately no newline: the PTY line discipline expands "\n" to "\r\n"
    # on write, which would make the drained byte count platform-dependent.
    payload = "héllo from tėst"
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    with bh as stream:
        stream.write(payload)
        stream.flush()
    # __exit__ joins the drainer thread, so drained_bytes is safe to read here.
    assert bh.drained_bytes == len(payload.encode("utf-8"))


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_pty_blackhole_resets_the_count_when_reused() -> None:
    """A completed PtyBlackhole context can count a subsequent drain."""
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    first_payload = "first"
    with bh as stream:
        stream.write(first_payload)
        stream.flush()
    assert bh.drained_bytes == len(first_payload.encode("utf-8"))

    second_payload = "sécond"
    with bh as stream:
        stream.write(second_payload)
        stream.flush()
    assert bh.drained_bytes == len(second_payload.encode("utf-8"))


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


def test_pty_blackhole_hides_drain_count_until_the_drainer_stops() -> None:
    """A timed-out join must not publish a drainer-owned counter."""
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    still_running = mock.Mock(spec=threading.Thread)
    still_running.is_alive.return_value = True
    bh._thread = still_running
    bh._drained_bytes = 42

    bh.__exit__(None, None, None)

    still_running.join.assert_called_once_with(timeout=5.0)
    assert bh._thread is still_running
    assert bh.drained_bytes is None


def test_pty_blackhole_publishes_count_after_a_late_drainer_exit() -> None:
    """A drainer that stops after the bounded join eventually publishes its count."""
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    eventually_stopped = mock.Mock(spec=threading.Thread)
    eventually_stopped.is_alive.side_effect = (True, False)
    bh._thread = eventually_stopped
    bh._drained_bytes = 42

    bh.__exit__(None, None, None)

    assert bh.drained_bytes == 42
    assert eventually_stopped.join.call_args_list == [
        mock.call(timeout=5.0),
        mock.call(),
    ]
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
