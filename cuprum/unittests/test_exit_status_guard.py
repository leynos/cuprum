"""Unit coverage for the opt-in process exit-status guard (#671).

The contract under test is the shutdown hook's treatment of each standard
stream: a stream whose reader has gone away is redirected or replaced, a
healthy stream and a deliberately closed one are left alone, and the hook can
never raise. Registration is exercised through a recording double so the test
process never installs the real ``atexit`` hook.

The end-to-end guarantee — that a real closed pipe no longer turns the
requested status into 120 — is proved in
``tests/behaviour/test_exit_status_guard_pipe.py``.
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
from cuprum.unittests._exit_status_guard_doubles import (
    _FailingDup2Os,
    _FakeStream,
    _ProxyReturningNoDescriptor,
    _ProxyWithNonCallableFileno,
    _ProxyWithoutFileno,
    _RecordingAtexit,
    _SelfDescriptorOs,
    assert_points_at_devnull,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@pytest.fixture(autouse=True)
def _reset_installed_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test's registration state independent."""
    monkeypatch.setattr(exit_status_guard, "_is_installed", False)


def test_dead_stream_is_redirected_and_keeps_its_identity(
    monkeypatch: pytest.MonkeyPatch,
    pipe_write_fd: int,
) -> None:
    """A dead descriptor is pointed at the null device, not replaced."""
    dead = _FakeStream(
        flush_error=BrokenPipeError("reader left"),
        descriptor=pipe_write_fd,
    )
    monkeypatch.setattr(sys, "stdout", dead)
    monkeypatch.setattr(sys, "stderr", _FakeStream())

    exit_status_guard._guard_standard_streams()

    assert sys.stdout is dead, "the stream object must keep its identity"
    assert_points_at_devnull(pipe_write_fd)


def test_healthy_stream_is_left_untouched(
    monkeypatch: pytest.MonkeyPatch,
    pipe_write_fd: int,
) -> None:
    """A stream that flushes cleanly keeps its identity and descriptor."""
    healthy = _FakeStream(descriptor=pipe_write_fd)
    monkeypatch.setattr(sys, "stdout", healthy)
    monkeypatch.setattr(sys, "stderr", _FakeStream())

    exit_status_guard._guard_standard_streams()

    assert sys.stdout is healthy, "a healthy stream must keep its identity"
    assert healthy.flushes == 1, "the hook must still probe the stream"
    assert not os.path.samestat(os.fstat(pipe_write_fd), Path(os.devnull).stat()), (
        "a healthy descriptor must not be redirected"
    )


def test_none_stream_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing standard stream is not an error."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    exit_status_guard._guard_standard_streams()

    assert sys.stdout is None, "a missing stream must stay missing"
    assert sys.stderr is None, "a missing stream must stay missing"


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
    assert isinstance(sink, exit_status_guard._NullTextSink), (
        "a descriptorless dead stream must be replaced by the null sink"
    )
    assert sink.writable() is True, "the null sink must accept text"
    assert sink.write("discarded") == len("discarded"), (
        "the null sink must report discarded text as written"
    )
    assert sink.flush() is None, "flushing the null sink must do nothing"


@pytest.mark.parametrize(
    "make_dead_stdout",
    [
        pytest.param(_ProxyWithoutFileno, id="no-fileno-attribute"),
        pytest.param(
            lambda: _FakeStream(
                flush_error=BrokenPipeError("reader left"),
                fileno_error=ValueError("proxy is closed"),
            ),
            id="closed-proxy",
        ),
        pytest.param(_ProxyReturningNoDescriptor, id="fileno-returns-none"),
        pytest.param(_ProxyWithNonCallableFileno, id="fileno-not-callable"),
    ],
)
def test_dead_stream_without_a_usable_descriptor_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
    make_dead_stdout: cabc.Callable[[], object],
) -> None:
    """Every way a proxy can decline a descriptor yields the null sink."""
    monkeypatch.setattr(sys, "stdout", make_dead_stdout())
    monkeypatch.setattr(sys, "stderr", _FakeStream())

    exit_status_guard._guard_standard_streams()

    assert isinstance(sys.stdout, exit_status_guard._NullTextSink), (
        "a dead stream without a usable descriptor must become the null sink"
    )


def test_both_dead_streams_are_guarded_independently(
    monkeypatch: pytest.MonkeyPatch,
    pipe_write_fd: int,
) -> None:
    """One descriptorless stream does not cost the other its redirect."""
    stdout = _FakeStream(
        flush_error=BrokenPipeError("reader left"),
        descriptor=pipe_write_fd,
    )
    stderr = _FakeStream(
        flush_error=BrokenPipeError("reader left"),
        fileno_error=io.UnsupportedOperation("no descriptor"),
    )
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    exit_status_guard._guard_standard_streams()

    assert sys.stdout is stdout, "the redirectable stream keeps its identity"
    assert_points_at_devnull(pipe_write_fd)
    assert isinstance(sys.stderr, exit_status_guard._NullTextSink), (
        "the descriptorless stream must be replaced by the null sink"
    )


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
    pipe_write_fd: int,
) -> None:
    """An unusable descriptor path still leaves a stream CPython can flush."""
    dead = _FakeStream(
        flush_error=BrokenPipeError("reader left"),
        descriptor=pipe_write_fd,
    )
    monkeypatch.setattr(sys, "stdout", dead)
    monkeypatch.setattr(sys, "stderr", _FakeStream())
    failing_os = _FailingDup2Os(os)
    monkeypatch.setattr(exit_status_guard, "os", failing_os)

    exit_status_guard._guard_standard_streams()

    assert failing_os.dup2_calls == 1, "the hook must have attempted the redirect"
    assert isinstance(sys.stdout, exit_status_guard._NullTextSink), (
        "a failed redirect must fall back to the null sink"
    )


def test_null_descriptor_reusing_the_stream_number_is_not_closed(
    monkeypatch: pytest.MonkeyPatch,
    pipe_write_fd: int,
) -> None:
    """A null device opened at the stream's own number must stay open."""
    dead = _FakeStream(
        flush_error=BrokenPipeError("reader left"),
        descriptor=pipe_write_fd,
    )
    monkeypatch.setattr(sys, "stdout", dead)
    monkeypatch.setattr(sys, "stderr", _FakeStream())
    reusing = _SelfDescriptorOs(os, pipe_write_fd)
    monkeypatch.setattr(exit_status_guard, "os", reusing)

    exit_status_guard._guard_standard_streams()

    assert sys.stdout is dead, "the stream must keep its identity"
    assert reusing.opened == [(os.devnull, os.O_WRONLY)], (
        "the guard must open the null device for the redirect"
    )
    assert reusing.closed == [], (
        "the descriptor that took the stream's number must stay open"
    )
    assert os.fstat(pipe_write_fd) is not None, (
        "the redirected descriptor must still be open"
    )


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
    assert exit_status_guard._is_installed is True, (
        "the flag must record the successful registration"
    )


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
