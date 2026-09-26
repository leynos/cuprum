"""Contract tests for capturing a child's output pipe before it exits.

The stalled-run diagnostic needs a child's stdout pipe name, but the kernel
releases a process's descriptors at exit, so ``/proc/<pid>/fd`` is already empty
by the time an exited child can be observed. The name therefore has to be
recorded while the child is still alive, and these tests pin that timing
contract: what the tracker captures on ``start``, and that the capture survives
the child's exit.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import time

from tests.behaviour._native_pipeline_hand_off import (
    _ProgressTracker,
    _snapshot_child,
    _stdout_parent_output,
)
from tests.helpers.process_state import child_pipes, process_state


class _StartEvent:
    """The subset of ``ExecEvent`` a ``start`` observation carries."""

    def __init__(self, pid: int | None) -> None:
        """Record the phase and the child's pid."""
        self.phase = "start"
        self.pid = pid
        self.tags: dict[str, object] = {}


def _pipe_name(fd: int) -> str:
    """Return the pipe name from this process's descriptor ``fd``."""
    return str(pathlib.Path(f"/proc/self/fd/{fd}").readlink())


def _fork_child_with_stdout(write_fd: int, ready_fd: int) -> int:
    """Fork a child whose stdout is ``write_fd``.

    The child signals on ``ready_fd`` only after its descriptors are arranged,
    so the returned pid is safe to observe without racing the child's startup.

    Returns
    -------
    int
        The forked child's pid.
    """
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child never returns
        os.dup2(write_fd, 1)  # the child's stdout becomes our pipe
        os.write(ready_fd, b"r")
        os.close(ready_fd)
        time.sleep(5)  # stay alive to be observed
        os._exit(0)
    return pid


def _await_exit(pid: int) -> None:
    """Wait for ``pid`` to reach a state procfs reports as exited."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if process_state(pid).exited:
            return
        time.sleep(0.01)


def test_the_tracker_records_the_stdout_pipe_of_a_started_child() -> None:
    """A ``start`` observation captures the child's stdout pipe while it lives."""
    read_fd, write_fd = os.pipe()
    ready_r, ready_w = os.pipe()
    pid = _fork_child_with_stdout(write_fd, ready_w)
    os.close(write_fd)
    os.close(ready_w)
    try:
        os.read(ready_r, 1)  # the child's stdout is only now in place
        tracker = _ProgressTracker()
        tracker.observe(_StartEvent(pid))  # type: ignore[arg-type]

        assert pid in tracker.pids, "a started child must be tracked by pid"
        assert tracker.stdout_target_by_pid.get(pid) == _pipe_name(read_fd), (
            "the child's stdout pipe name must be recorded at start, "
            f"got {tracker.stdout_target_by_pid!r}"
        )
    finally:
        os.close(ready_r)
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        os.close(read_fd)


def test_the_captured_pipe_name_outlives_the_child() -> None:
    """A name recorded on start still resolves a parent read end after exit.

    This is the whole reason the capture is deferred to ``start``: sampled at
    stall time the child's own descriptors are gone, so a naive lookup would
    find nothing precisely when the diagnostic needs the evidence.
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child never returns
        os.close(read_fd)
        os.write(write_fd, b"uncollected output")
        os._exit(0)
    os.close(write_fd)
    try:
        tracker = _ProgressTracker()
        tracker.pids.add(pid)
        tracker.stdout_target_by_pid[pid] = _pipe_name(read_fd)
        _await_exit(pid)

        pipes_after_exit = child_pipes(pid)
        snapshot = _snapshot_child(pid, tracker)

        assert not pipes_after_exit, (
            "the premise of the capture: an exited child exposes no descriptors, "
            f"found {pipes_after_exit!r}"
        )
        assert snapshot.process.exited, "the child must be observed as exited"
        assert snapshot.stdout_parent is not None, (
            "the recorded pipe name must still resolve the parent's read end"
        )
        assert snapshot.stdout_parent.pending_bytes == len(b"uncollected output"), (
            "the uncollected payload must be measurable, got "
            f"{snapshot.stdout_parent!r}"
        )
    finally:
        with contextlib.suppress(ChildProcessError):
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        os.close(read_fd)


def test_an_unrecorded_child_yields_no_parent_output() -> None:
    """A child with no recorded pipe name contributes no stdout evidence."""
    assert _stdout_parent_output(None) is None, (
        "an unknown pipe name cannot resolve a parent read end"
    )


def test_a_name_this_process_no_longer_holds_yields_no_parent_output() -> None:
    """A pipe whose parent end was closed is reported as absent, not as zero."""
    read_fd, write_fd = os.pipe()
    target = _pipe_name(read_fd)
    os.close(read_fd)
    os.close(write_fd)

    assert _stdout_parent_output(target) is None, (
        "a closed read end must be reported as absent rather than as empty"
    )
