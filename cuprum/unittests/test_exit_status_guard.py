"""Unit coverage for the opt-in process exit-status guard (#671).

The contract under test is the shutdown hook's treatment of each standard
stream: a stream whose reader has gone away is redirected or replaced, a
healthy stream and a deliberately closed one are left alone, and the hook can
never raise. Registration is exercised through a recording double so the test
process never installs the real ``atexit`` hook.

The end-to-end guarantee — that a real closed pipe no longer turns the
requested status into 120 — is proved in
``tests/behaviour/test_exit_status_guard.py``.
"""

from __future__ import annotations

import io
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] - isolates import side effects.
import sys
import threading
import typing as typ
from pathlib import Path

import pytest

from cuprum import exit_status_guard

if typ.TYPE_CHECKING:
    import types


class _FakeStream:
    """A standard-stream double that records what the hook asks of it.

    Attributes
    ----------
    flushes:
        How many times ``flush`` was called.
    """

    def __init__(
        self,
        *,
        flush_error: BaseException | None = None,
        descriptor: int | None = None,
        fileno_error: BaseException | None = None,
    ) -> None:
        """Configure the failure each call reports, if any."""
        self.flushes = 0
        self._flush_error = flush_error
        self._descriptor = descriptor
        self._fileno_error = fileno_error

    def flush(self) -> None:
        """Record the flush, then raise the configured failure."""
        self.flushes += 1
        if self._flush_error is not None:
            raise self._flush_error

    def fileno(self) -> int:
        """Return the configured descriptor, or raise the configured failure."""
        if self._fileno_error is not None:
            raise self._fileno_error
        if self._descriptor is None:
            msg = "test double has no descriptor"
            raise AssertionError(msg)
        return self._descriptor


class _RecordingAtexit:
    """A stand-in for the ``atexit`` module that records registrations."""

    def __init__(self) -> None:
        """Start with nothing registered."""
        self.registered: list[object] = []

    def register(self, func: object, /, *args: object, **kwargs: object) -> None:
        """Record one registration without installing it."""
        self.registered.append(func)


class _FailingDup2Os:
    """An ``os`` proxy whose ``dup2`` always fails."""

    def __init__(self, real: types.ModuleType) -> None:
        """Wrap the real module so every other attribute still works."""
        self._real = real
        self.dup2_calls = 0

    def __getattr__(self, name: str) -> object:
        """Delegate every attribute the guard does not override."""
        return getattr(self._real, name)

    def dup2(self, source: int, target: int) -> None:
        """Record the attempt and fail the way an unusable descriptor would."""
        self.dup2_calls += 1
        msg = "dup2 refused the descriptor"
        raise OSError(msg)


@pytest.fixture(autouse=True)
def _reset_installed_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test's registration state independent."""
    monkeypatch.setattr(exit_status_guard, "_is_installed", False)


def _assert_points_at_devnull(descriptor: int) -> None:
    """Fail unless *descriptor* now refers to the null device."""
    assert os.path.samestat(os.fstat(descriptor), Path(os.devnull).stat()), (
        f"descriptor {descriptor} should have been redirected to os.devnull"
    )


def test_dead_stream_is_redirected_and_keeps_its_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead descriptor is pointed at the null device, not replaced."""
    read_fd, write_fd = os.pipe()
    try:
        dead = _FakeStream(
            flush_error=BrokenPipeError("reader left"),
            descriptor=write_fd,
        )
        monkeypatch.setattr(sys, "stdout", dead)
        monkeypatch.setattr(sys, "stderr", _FakeStream())

        exit_status_guard._guard_standard_streams()

        assert sys.stdout is dead, "the stream object must keep its identity"
        _assert_points_at_devnull(write_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_healthy_stream_is_left_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream that flushes cleanly keeps its identity and descriptor."""
    read_fd, write_fd = os.pipe()
    try:
        healthy = _FakeStream(descriptor=write_fd)
        monkeypatch.setattr(sys, "stdout", healthy)
        monkeypatch.setattr(sys, "stderr", _FakeStream())

        exit_status_guard._guard_standard_streams()

        assert sys.stdout is healthy
        assert healthy.flushes == 1, "the hook must still probe the stream"
        assert not os.path.samestat(os.fstat(write_fd), Path(os.devnull).stat()), (
            "a healthy descriptor must not be redirected"
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_none_stream_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing standard stream is not an error."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    exit_status_guard._guard_standard_streams()

    assert sys.stdout is None
    assert sys.stderr is None


def test_descriptorless_dead_stream_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead stream with no descriptor is replaced by a safe sink."""
    dead = _FakeStream(
        flush_error=BrokenPipeError("reader left"),
        fileno_error=io.UnsupportedOperation("no descriptor"),
    )
    monkeypatch.setattr(sys, "stdout", dead)
    monkeypatch.setattr(sys, "stderr", _FakeStream())

    exit_status_guard._guard_standard_streams()

    sink = sys.stdout
    assert isinstance(sink, exit_status_guard._NullTextSink)
    assert sink.writable() is True
    assert sink.write("discarded") == len("discarded")
    assert sink.flush() is None


def test_both_dead_streams_are_guarded_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One descriptorless stream does not cost the other its redirect."""
    read_fd, write_fd = os.pipe()
    try:
        stdout = _FakeStream(
            flush_error=BrokenPipeError("reader left"),
            descriptor=write_fd,
        )
        stderr = _FakeStream(
            flush_error=BrokenPipeError("reader left"),
            fileno_error=io.UnsupportedOperation("no descriptor"),
        )
        monkeypatch.setattr(sys, "stdout", stdout)
        monkeypatch.setattr(sys, "stderr", stderr)

        exit_status_guard._guard_standard_streams()

        assert sys.stdout is stdout
        _assert_points_at_devnull(write_fd)
        assert isinstance(sys.stderr, exit_status_guard._NullTextSink)
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ValueError("stream is closed"), id="deliberately-closed"),
        pytest.param(OSError("device unreachable"), id="other-oserror"),
    ],
)
def test_non_broken_pipe_failures_leave_the_stream_untouched(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """Only ``BrokenPipeError`` means the reader is gone."""
    stream = _FakeStream(flush_error=error, descriptor=0)
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", _FakeStream())

    exit_status_guard._guard_standard_streams()

    assert sys.stdout is stream, "a different defect must not be masked"


def test_dup2_failure_falls_back_to_the_null_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unusable descriptor path still leaves a stream CPython can flush."""
    read_fd, write_fd = os.pipe()
    try:
        dead = _FakeStream(
            flush_error=BrokenPipeError("reader left"),
            descriptor=write_fd,
        )
        monkeypatch.setattr(sys, "stdout", dead)
        monkeypatch.setattr(sys, "stderr", _FakeStream())
        failing_os = _FailingDup2Os(os)
        monkeypatch.setattr(exit_status_guard, "os", failing_os)

        exit_status_guard._guard_standard_streams()

        assert failing_os.dup2_calls == 1
        assert isinstance(sys.stdout, exit_status_guard._NullTextSink)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_install_registers_exactly_one_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent installs share one registration."""
    recorder = _RecordingAtexit()
    monkeypatch.setattr(exit_status_guard, "atexit", recorder)

    threads = [
        threading.Thread(target=exit_status_guard.install_exit_status_guard)
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert recorder.registered == [exit_status_guard._guard_standard_streams], (
        "exactly one hook must be registered, however many callers race"
    )
    assert exit_status_guard._is_installed is True


def test_importing_cuprum_installs_nothing() -> None:
    """A fresh interpreter reports the guard as not installed."""
    script = (
        "import cuprum, cuprum.exit_status_guard as guard\nprint(guard._is_installed)\n"
    )
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed interpreter invocation.
        [sys.executable, "-c", script],
        capture_output=True,
        check=True,
        text=True,
        timeout=15.0,
    )

    assert completed.stdout.strip() == "False", (
        "importing cuprum must not install the shutdown guard"
    )
