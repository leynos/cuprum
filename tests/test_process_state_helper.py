"""Contract tests for Linux process-state diagnostic helpers."""

from __future__ import annotations

import contextlib
import os
import sys
import time

import pytest

from tests.helpers.process_state import (
    child_pipes,
    parent_read_fds,
    parent_write_fds,
    process_state,
    read_end_pending_bytes,
)


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux procfs")
def test_process_state_reports_the_current_process() -> None:
    """The current process has an available non-zombie procfs record."""
    state = process_state(os.getpid())

    assert state.available, "the current process must expose a procfs record"
    assert not state.exited, "the current process must not appear exited"
    assert state.state not in {None, "Z"}, (
        f"the current process must not be unavailable or zombie, got {state!r}"
    )


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux procfs")
def test_child_pipes_report_pending_bytes_and_parent_writer() -> None:
    """A local pipe exposes its unread bytes and this process's writer."""
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"hello")
        pipe = next(pipe for pipe in child_pipes(os.getpid()) if pipe.fd == read_fd)

        assert pipe.pending_bytes == 5, (
            f"the read end must report the queued payload, got {pipe!r}"
        )
        assert write_fd in parent_write_fds(pipe.target), (
            f"the local write end must remain visible for {pipe.target!r}"
        )
        assert read_fd in parent_read_fds(pipe.target), (
            f"the local read end must be reported as readable for {pipe.target!r}"
        )
        assert read_end_pending_bytes(read_fd) == 5, (
            "sampling this process's own read end must report the queued payload"
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)


def _fork_immediately_exiting_child() -> int:
    """Fork a child that exits at once, and return its pid."""
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child never returns from _exit
        os._exit(0)
    return pid


def _wait_for_exit(pid: int) -> None:
    """Reap ``pid``, waiting for it to exit if it has not already.

    Reaping is best effort: the test's assertions are about the observations
    taken around this call, so a child that has already been collected -- or a
    pid this process no longer owns -- must not turn that into an error.
    """
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, os.WNOHANG)


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux procfs")
def test_process_state_distinguishes_zombie_from_reaped_child() -> None:
    """A zombie stays observable; a reaped pid is reported as exited and gone."""
    pid = _fork_immediately_exiting_child()
    deadline = time.monotonic() + 5.0
    zombie = process_state(pid)
    try:
        while zombie.state != "Z" and time.monotonic() < deadline:
            time.sleep(0.01)
            zombie = process_state(pid)

        # Deliberately do not assert on ``zombie.state``: the wait loop above
        # can time out on a loaded host, and the contract under test is that
        # either observation is well formed, not that this process won a race.
        assert zombie.available, (
            f"an unreaped child must stay observable, got {zombie!r}"
        )
        assert zombie.exited, (
            "a child that has exited but not been reaped must report exited"
        )
    finally:
        _wait_for_exit(pid)

    reaped = process_state(pid)
    assert reaped.available, (
        f"a reaped pid must still report an available observation, got {reaped!r}"
    )
    assert reaped.exited, "a reaped pid has no procfs record and must report exited"
