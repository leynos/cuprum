"""Contract tests for native pipeline hand-off stall classification."""

from __future__ import annotations

from tests.behaviour._native_pipeline_hand_off import (
    HandOffVerdict,
    _ChildStallSnapshot,
    _classify_stall,
    _StallSnapshot,
)
from tests.helpers.process_state import ChildPipe, ProcessState


def _snapshot(
    *,
    state: str | None,
    exited: bool,
    wchan: str | None,
    pending_bytes: int | None = None,
    writers: tuple[int, ...] = (),
) -> _StallSnapshot:
    """Build one child snapshot with the supplied discriminator evidence."""
    child = _ChildStallSnapshot(
        ProcessState(42, state, exited, wchan, available=True),
        (ChildPipe(0, "pipe:[42]", pending_bytes),),
        writers,
    )
    return _StallSnapshot((child,), ("pipeline-wait",), "test stall")


def test_parent_writer_waiting_on_pipe_read_is_a_hung_hand_off() -> None:
    """A parent writer prevents the downstream reader from receiving EOF."""
    snapshot = _snapshot(state="S", exited=False, wchan="pipe_read", writers=(10,))

    assert _classify_stall(snapshot) is HandOffVerdict.HUNG_HANDOFF, (
        "a child blocked in pipe_read while its parent holds a writer must fail"
    )


def test_runnable_child_is_host_starvation() -> None:
    """A runnable child leaves the host, rather than a hand-off, accountable."""
    snapshot = _snapshot(state="R", exited=False, wchan=None)

    assert _classify_stall(snapshot) is HandOffVerdict.HOST_STARVATION, (
        "a runnable child must make the deadline a non-verdict"
    )


def test_exited_children_with_unread_output_are_a_hung_hand_off() -> None:
    """Exited children and pending bytes prove completion was not observed."""
    snapshot = _snapshot(state=None, exited=True, wchan=None, pending_bytes=5)

    assert _classify_stall(snapshot) is HandOffVerdict.HUNG_HANDOFF, (
        "uncollected output after child exit must fail as a missed hand-off"
    )
