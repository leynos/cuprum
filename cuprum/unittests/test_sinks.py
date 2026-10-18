"""Tests for the tee destinations and blackhole sinks.

The presentation-adapter tests live beside their subjects: the shared session
lifecycle in ``test_sink_lifecycle.py`` and the GitHub Actions adapter in
``test_sinks_github_actions.py``.
"""

from __future__ import annotations

import errno
import os
import termios
import threading
import typing as typ
from unittest import mock

import pytest

from benchmarks import (
    PtyBlackholeDrainError,
    PtyBlackholeStateError,
    _pty_setup,
    pty_blackhole,
    sinks,
)


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_pty_blackhole_enter_cleans_up_when_text_stream_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PTY sink setup closes open descriptors if stream setup fails."""
    master_fd, slave_fd = os.openpty()
    monkeypatch.setattr(
        _pty_setup.pty,
        "openpty",
        lambda: (master_fd, slave_fd),
    )

    def fail_text_wrapper(*_args: object, **_kwargs: object) -> typ.NoReturn:
        """Raise RuntimeError to simulate text stream setup failing."""
        msg = "text wrapper failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(_pty_setup.io, "TextIOWrapper", fail_text_wrapper)
    blackhole = sinks.PtyBlackhole(encoding="utf-8", errors="replace")

    with pytest.raises(RuntimeError, match="text wrapper failed"), blackhole:
        pass

    def fstat_error(fd: int) -> OSError:
        """Return the OSError raised when fstat is called on a closed fd."""
        try:
            os.fstat(fd)
        except OSError as exc:
            return exc
        return pytest.fail(f"expected closed fd {fd} to raise OSError")

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
        assert stream.isatty()


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_pty_blackhole_drains_written_bytes() -> None:
    """Data written to the PtyBlackhole slave FD is consumed by the drainer."""
    payload = "héllo from tėst" + "x" * (128 * 1024)
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    with bh as stream:
        stream.write(payload)
        stream.flush()
    # __exit__ joins the drainer thread, so drained_bytes is safe to read here.
    assert bh.drained_bytes == len(payload.encode("utf-8")), (
        "the drainer must publish exactly the UTF-8 byte count after exit"
    )


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_pty_blackhole_drains_output_before_closing_slave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the slave open until the reader acknowledges its accepted bytes."""
    blackhole = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    master_fds: list[int] = []
    original_openpty = _pty_setup.pty.openpty
    original_read = os.read
    original_join = pty_blackhole.threading.Thread.join
    reader_entered = threading.Event()
    release_reader = threading.Event()
    join_entered = threading.Event()

    def capture_openpty() -> tuple[int, int]:
        """Capture the master descriptor for the deterministic read gate."""
        master_fd, slave_fd = original_openpty()
        master_fds.append(master_fd)
        return master_fd, slave_fd

    def gated_read(fd: int, count: int) -> bytes:
        """Hold the master reader until context teardown reaches its join."""
        if fd == master_fds[0]:
            reader_entered.set()
            if not release_reader.wait(timeout=5.0):
                msg = "reader gate was not released"
                raise TimeoutError(msg)
        return original_read(fd, count)

    def release_at_join(
        thread: threading.Thread,
        timeout: float | None = None,
    ) -> None:
        """Confirm teardown has not closed the slave before acknowledging."""
        if thread is blackhole._thread:
            os.fstat(slave_fd)
            join_entered.set()
            release_reader.set()
        original_join(thread, timeout)

    monkeypatch.setattr(_pty_setup.pty, "openpty", capture_openpty)
    monkeypatch.setattr(sinks.os, "read", gated_read)
    monkeypatch.setattr(pty_blackhole.threading.Thread, "join", release_at_join)
    with blackhole as stream:
        slave_fd = stream.fileno()
        stream.write("first")
        stream.flush()
        assert reader_entered.wait(timeout=5.0), "drainer must reach its gated read"

    assert join_entered.is_set(), "context exit must wait for reader acknowledgement"
    assert blackhole.drained_bytes == len(b"first")


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_pty_blackhole_counts_translated_newlines() -> None:
    """Count the PTY's configured newline expansion in the acknowledged bytes."""
    if not getattr(termios, "ONLCR", 0):
        pytest.skip("termios does not provide ONLCR")
    payload = "héllo\nthere\n"
    blackhole = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    with blackhole as stream:
        stream.write(payload)
    expected = payload.encode("utf-8").replace(b"\n", b"\r\n")
    assert blackhole.drained_bytes == len(expected)


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_pty_blackhole_does_not_publish_count_after_reader_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected master read error cannot masquerade as a full drain."""
    blackhole = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    master_fds: list[int] = []
    original_openpty = _pty_setup.pty.openpty
    original_read = os.read
    reader_entered = threading.Event()

    def capture_openpty() -> tuple[int, int]:
        """Capture the descriptor whose read failure is under test."""
        master_fd, slave_fd = original_openpty()
        master_fds.append(master_fd)
        return master_fd, slave_fd

    def fail_master_read(fd: int, count: int) -> bytes:
        """Raise EIO only for the PTY master, preserving control-pipe reads."""
        if fd == master_fds[0]:
            reader_entered.set()
            raise OSError(errno.EIO, "injected PTY reader failure")
        return original_read(fd, count)

    monkeypatch.setattr(_pty_setup.pty, "openpty", capture_openpty)
    monkeypatch.setattr(sinks.os, "read", fail_master_read)

    def fail_after_reader_read() -> None:
        """Raise the sink's drain error after the injected reader failure."""
        with blackhole as stream:
            stream.write("failure")
            stream.flush()
            assert reader_entered.wait(timeout=5.0), (
                "drainer must attempt the master read"
            )

    with pytest.raises(PtyBlackholeDrainError, match="failed before acknowledging"):
        fail_after_reader_read()

    assert blackhole.drained_bytes is None


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
    assert bh.drained_bytes == len(first_payload.encode("utf-8")), (
        "the first completed context must publish its UTF-8 byte count"
    )

    second_payload = "sécond"
    with bh as stream:
        stream.write(second_payload)
        stream.flush()
    assert bh.drained_bytes == len(second_payload.encode("utf-8")), (
        "reusing the sink must publish only the second context's byte count"
    )


@pytest.mark.skipif(
    not hasattr(os, "openpty"),
    reason="os.openpty is unavailable on this platform",
)
def test_pty_blackhole_exit_clears_internal_state() -> None:
    """PtyBlackhole.__exit__ clears _master_fd, _slave, and _thread."""
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    with bh:
        pass
    assert bh._master_fd is None, "exit must clear the PTY master descriptor"
    assert bh._slave is None, "exit must clear the PTY slave stream"
    assert bh._thread is None, "exit must clear a joined drainer thread"


def test_pty_blackhole_hides_drain_count_until_the_drainer_stops() -> None:
    """A timed-out join must not publish a drainer-owned counter."""
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    still_running = mock.Mock(spec=threading.Thread)
    still_running.is_alive.return_value = True
    bh._thread = still_running
    bh._drained_bytes = 42
    bh._drain_target = 42

    with pytest.raises(PtyBlackholeDrainError, match="timed out"):
        bh.__exit__(None, None, None)

    still_running.join.assert_called_once_with(timeout=5.0)
    assert bh._thread is still_running, (
        "a timed-out join must retain the drainer for a later lifecycle boundary"
    )
    assert bh.drained_bytes is None, (
        "the count must remain unavailable while the drainer is still running"
    )


def test_pty_blackhole_publishes_count_after_a_late_drainer_exit() -> None:
    """A drainer that stops after the bounded join eventually publishes its count."""
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    eventually_stopped = mock.Mock(spec=threading.Thread)
    eventually_stopped.is_alive.side_effect = (True, False, False)
    bh._thread = eventually_stopped
    bh._drained_bytes = 42
    bh._drain_target = 42

    with pytest.raises(PtyBlackholeDrainError, match="timed out"):
        bh.__exit__(None, None, None)

    bh._drainer_finished.set()

    assert bh.drained_bytes == 42, (
        "a completed drainer must publish its count without mutating lifecycle state"
    )
    assert bh._thread is eventually_stopped, (
        "reading the count must not join or clear the retained drainer"
    )
    bh._publish_finished_drainer()
    assert eventually_stopped.join.call_args_list == [
        mock.call(timeout=5.0),
        mock.call(),
    ], "a late drainer must be joined only at the explicit lifecycle boundary"
    assert bh._thread is None, "the lifecycle boundary must clear a joined drainer"


def test_pty_blackhole_rejects_reuse_while_the_drainer_is_running() -> None:
    """PtyBlackhole reports an active previous drainer with its domain error."""
    bh = sinks.PtyBlackhole(encoding="utf-8", errors="replace")
    still_running = mock.Mock(spec=threading.Thread)
    still_running.is_alive.return_value = True
    bh._thread = still_running

    with pytest.raises(PtyBlackholeStateError, match="cannot reuse PtyBlackhole"), bh:
        pass


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
