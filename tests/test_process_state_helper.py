"""Contract tests for Linux process-state diagnostic helpers."""

from __future__ import annotations

import contextlib
import os
import sys

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
    """Reap ``pid``, blocking until it exits if it has not already.

    The wait blocks on purpose. ``WNOHANG`` would return without reaping a
    child that was still live -- the case when the caller's own ``waitid``
    raised first -- leaving a zombie behind for the rest of the session. The
    child under test calls ``os._exit(0)`` immediately, so the block is
    bounded.

    Reaping is still best effort: the test's assertions are about the
    observations taken around this call, so a child that has already been
    collected -- or a pid this process no longer owns -- must not turn that
    into an error.
    """
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, 0)


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux procfs")
def test_process_state_distinguishes_zombie_from_reaped_child() -> None:
    """A zombie stays observable; a reaped pid is reported as exited and gone."""
    pid = _fork_immediately_exiting_child()
    try:
        # ``WNOWAIT`` reaps nothing: it blocks until the child has exited, then
        # returns and leaves the zombie in place to be observed. The kernel
        # publishes the zombie state before it wakes this waiter, so the
        # unreaped observation is deterministic -- where polling for ``"Z"``
        # would make the assertion depend on winning a race on a loaded host.
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)

        zombie = process_state(pid)
        assert zombie.available, (
            f"an unreaped child must stay observable, got {zombie!r}"
        )
        assert zombie.state == "Z", (
            f"an unreaped child must report the zombie state, got {zombie!r}"
        )
        assert zombie.exited, "a zombie must be reported as exited"
        assert not child_pipes(pid), (
            f"a zombie holds no descriptors to report, got {child_pipes(pid)!r}"
        )
    finally:
        _wait_for_exit(pid)

    reaped = process_state(pid)
    assert reaped.available, (
        f"a reaped pid must still report an available observation, got {reaped!r}"
    )
    assert reaped.state is None, (
        f"a reaped pid has no procfs record, so it must report no state, got {reaped!r}"
    )
    assert reaped.exited, "a reaped pid has no procfs record and must report exited"
    assert not child_pipes(pid), (
        f"a reaped pid holds no descriptors, got {child_pipes(pid)!r}"
    )
