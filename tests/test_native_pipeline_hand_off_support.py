"""Contract tests for native pipeline hand-off stall classification."""

from __future__ import annotations

from tests.behaviour._native_pipeline_hand_off import _StallSnapshot
from tests.behaviour._native_pipeline_stall import (
    HandOffVerdict,
    _ChildStallSnapshot,
    _classify_stall,
    _ParentOutput,
)
from tests.helpers.process_state import ProcessState


def _child(
    process: ProcessState,
    *,
    stage_index: int | None = 1,
    stdin_parent_writers: tuple[int, ...] = (),
    stdout_parent: _ParentOutput | None = None,
) -> _ChildStallSnapshot:
    """Build one child snapshot with the supplied discriminator evidence."""
    return _ChildStallSnapshot(
        process,
        (),
        stdin_parent_writers,
        stage_index,
        stdout_parent,
    )


def _snapshot(
    child: _ChildStallSnapshot,
    *,
    task_names: tuple[str, ...] = (),
) -> _StallSnapshot:
    """Build a one-child stall snapshot with the supplied task evidence.

    ``task_names`` defaults to empty so a test isolates the rule it names.
    A pending task alongside a non-runnable child is itself ``HUNG_HANDOFF``
    evidence under the rule that outlived-child tasks imply a stall, and that
    rule is checked on its own terms elsewhere; leaving it switched on here
    would let it mask the rule under test.

    Returns
    -------
    _StallSnapshot
        A snapshot holding just ``child`` and the supplied task names.
    """
    return _StallSnapshot((child,), task_names, "test stall")


def _exited_stage(stage_index: int) -> _ChildStallSnapshot:
    """Build the process evidence for an upstream stage that has exited."""
    return _child(
        ProcessState(900 + stage_index, None, exited=True, wchan=None, available=True),
        stage_index=stage_index,
    )


def test_pending_tasks_without_a_runnable_child_are_a_hung_hand_off() -> None:
    """A pending task beside a non-runnable child proves the tasks outlived it."""
    snapshot = _snapshot(
        _child(ProcessState(42, "S", exited=False, wchan=None, available=True)),
        task_names=("native-pipeline-hand-off:pipeline_wait",),
    )

    assert _classify_stall(snapshot) is HandOffVerdict.HUNG_HANDOFF, (
        "pending tasks with no runnable child must fail as a stall"
    )


def test_pending_tasks_beside_a_runnable_child_are_host_starvation() -> None:
    """A child still awaiting CPU service accounts for the pending tasks."""
    snapshot = _snapshot(
        _child(ProcessState(42, "R", exited=False, wchan=None, available=True)),
        task_names=("native-pipeline-hand-off:pipeline_wait",),
    )

    assert _classify_stall(snapshot) is HandOffVerdict.HOST_STARVATION, (
        "a runnable child must keep the pending tasks from implying a stall"
    )


def test_pending_tasks_beside_a_disk_wait_child_are_host_starvation() -> None:
    """Uninterruptible I/O is host service, not a missed hand-off."""
    snapshot = _snapshot(
        _child(ProcessState(42, "D", exited=False, wchan=None, available=True)),
        task_names=("native-pipeline-hand-off:pipeline_wait",),
    )

    assert _classify_stall(snapshot) is HandOffVerdict.HOST_STARVATION, (
        "a child in uninterruptible I/O must make the deadline a non-verdict"
    )


def test_pending_tasks_without_children_are_not_a_verdict() -> None:
    """No child evidence means no verdict, whatever the task list says."""
    snapshot = _StallSnapshot((), ("native-pipeline-hand-off:pipeline_wait",), "test")

    assert _classify_stall(snapshot) is HandOffVerdict.HOST_STARVATION, (
        "the pending-task rule needs a child it can prove the tasks outlived"
    )


def test_unavailable_process_evidence_is_host_starvation() -> None:
    """A host without procfs yields no evidence, so it cannot yield a verdict."""
    snapshot = _snapshot(
        _child(ProcessState(42, None, exited=False, wchan=None, available=False)),
    )

    assert _classify_stall(snapshot) is HandOffVerdict.HOST_STARVATION, (
        "an unavailable record must not be read as a missed hand-off"
    )


def test_unknown_pending_bytes_are_not_uncollected_output() -> None:
    """An unsupported ioctl leaves the byte count unknown, not non-zero."""
    snapshot = _snapshot(
        _child(
            ProcessState(42, None, exited=True, wchan=None, available=True),
            stdout_parent=_ParentOutput(9, "pipe:[43]", None),
        ),
    )

    assert _classify_stall(snapshot) is HandOffVerdict.HOST_STARVATION, (
        "an unknown byte count must not be reported as uncollected output"
    )


def test_running_children_without_a_read_end_are_host_starvation() -> None:
    """A live child and drained pipes leave the host accountable."""
    snapshot = _snapshot(
        _child(ProcessState(42, "S", exited=False, wchan="do_wait", available=True)),
    )

    assert _classify_stall(snapshot) is HandOffVerdict.HOST_STARVATION, (
        "no rule may fire on a snapshot that carries no evidence"
    )


def test_parent_writer_waiting_on_pipe_read_is_a_hung_hand_off() -> None:
    """A parent writer prevents the downstream reader from receiving EOF."""
    blocked = _child(
        ProcessState(42, "S", exited=False, wchan="pipe_read", available=True),
        stage_index=1,
        stdin_parent_writers=(10,),
    )
    snapshot = _StallSnapshot((blocked, _exited_stage(0)), (), "test stall")

    assert _classify_stall(snapshot) is HandOffVerdict.HUNG_HANDOFF, (
        "a child blocked in pipe_read while its parent holds a writer must fail"
    )


def test_parent_writer_rule_needs_an_exited_upstream_stage() -> None:
    """A writer from a stage that is still running is not a missed close."""
    blocked = _child(
        ProcessState(42, "S", exited=False, wchan="pipe_read", available=True),
        stage_index=1,
        stdin_parent_writers=(10,),
    )
    running_upstream = _child(
        ProcessState(900, "S", exited=False, wchan="do_exit", available=True),
        stage_index=0,
    )
    snapshot = _StallSnapshot((blocked, running_upstream), (), "test stall")

    assert _classify_stall(snapshot) is HandOffVerdict.HOST_STARVATION, (
        "an upstream stage that has not exited has not missed its hand-off"
    )


def test_parent_writer_rule_needs_the_immediate_upstream_stage() -> None:
    """An exited stage that does not feed this child cannot justify the rule."""
    blocked = _child(
        ProcessState(42, "S", exited=False, wchan="pipe_read", available=True),
        stage_index=2,
        stdin_parent_writers=(10,),
    )
    unrelated = _exited_stage(0)
    snapshot = _StallSnapshot((blocked, unrelated), (), "test stall")

    assert _classify_stall(snapshot) is HandOffVerdict.HOST_STARVATION, (
        "only the edge this child reads from can prove a missed close"
    )


def test_parent_writer_rule_ignores_an_unrelated_runnable_stage() -> None:
    """A runnable stage elsewhere must not hide a genuine missed hand-off."""
    blocked = _child(
        ProcessState(42, "S", exited=False, wchan="pipe_read", available=True),
        stage_index=1,
        stdin_parent_writers=(10,),
    )
    unrelated = _child(
        ProcessState(43, "R", exited=False, wchan=None, available=True),
        stage_index=3,
    )
    snapshot = _StallSnapshot((blocked, _exited_stage(0), unrelated), (), "test stall")

    assert _classify_stall(snapshot) is HandOffVerdict.HUNG_HANDOFF, (
        "a runnable stage on another edge must not suppress the verdict"
    )


def test_runnable_child_is_host_starvation() -> None:
    """A runnable child leaves the host, rather than a hand-off, accountable."""
    snapshot = _snapshot(
        _child(ProcessState(42, "R", exited=False, wchan=None, available=True)),
    )

    assert _classify_stall(snapshot) is HandOffVerdict.HOST_STARVATION, (
        "a runnable child must make the deadline a non-verdict"
    )


def test_exited_children_with_unread_output_are_a_hung_hand_off() -> None:
    """An exited child and pending parent-held bytes prove lost output."""
    snapshot = _snapshot(
        _child(
            ProcessState(42, None, exited=True, wchan=None, available=True),
            stdout_parent=_ParentOutput(9, "pipe:[43]", 5),
        ),
    )

    assert _classify_stall(snapshot) is HandOffVerdict.HUNG_HANDOFF, (
        "uncollected output after child exit must fail as a missed hand-off"
    )


def test_exited_child_with_drained_output_is_host_starvation() -> None:
    """A parent read end that has been drained is not evidence of lost output."""
    snapshot = _snapshot(
        _child(
            ProcessState(42, None, exited=True, wchan=None, available=True),
            stdout_parent=_ParentOutput(9, "pipe:[43]", 0),
        ),
    )

    assert _classify_stall(snapshot) is HandOffVerdict.HOST_STARVATION, (
        "a drained read end must not be reported as uncollected output"
    )
