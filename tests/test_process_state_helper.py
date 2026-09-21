"""Contract tests for Linux process-state diagnostic helpers."""

from __future__ import annotations

import os
import sys

import pytest

from tests.helpers.process_state import child_pipes, parent_write_fds, process_state


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
    finally:
        os.close(read_fd)
        os.close(write_fd)
