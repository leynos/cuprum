"""Contract tests for the rendered native hand-off stall diagnostic.

The classifier's verdicts are pinned by example in
:mod:`tests.test_native_pipeline_hand_off_support`; this module pins the other
half of the same contract, the *rendered* message a maintainer actually reads
out of a CI failure. The two are independent: every rule in
:func:`~tests.behaviour._native_pipeline_stall._classify_stall` could keep
returning the right verdict while the diagnostic quietly stopped naming the
attempt, the pipe evidence, or the pending tasks, and no other test in the
repository would notice.

Reports are built from fixed synthetic evidence rather than from a captured
stall, so the snapshot is deterministic: the descriptors, pids, and pipe
targets are the ones these fixtures declare, and the two timing fields are
formatted from fixed values. The pipeline is rendered through the same
``__repr__`` a real attempt would use, with the workspace path redacted.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

import pytest

from tests.behaviour._native_pipeline_hand_off import _StallSnapshot
from tests.behaviour._native_pipeline_liveness import LivenessProgress
from tests.behaviour._native_pipeline_stall import (
    HandOffVerdict,
    _ChildStallSnapshot,
    _format_stall,
    _ParentOutput,
    _StallReport,
)
from tests.helpers.process_state import ChildPipe, ProcessState

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion


def _exited_child_with_parent_output(
    *,
    pid: int = 4242,
    stage_index: int = 0,
    pending_bytes: int | None = 5,
) -> _ChildStallSnapshot:
    """Build an exited child whose parent read end still holds bytes.

    Returns
    -------
    _ChildStallSnapshot
        An exited child with uncollected output on its parent's read end.
    """
    return _ChildStallSnapshot(
        ProcessState(pid, None, exited=True, wchan=None, available=True),
        (ChildPipe(1, "pipe:[43]", pending_bytes),),
        (),
        stage_index,
        _ParentOutput(9, "pipe:[43]", pending_bytes),
    )


def _blocked_on_parent_stdin(
    *,
    pid: int = 4243,
    stage_index: int = 1,
) -> _ChildStallSnapshot:
    """Build a child waiting in ``pipe_read`` on a parent-held writer.

    Returns
    -------
    _ChildStallSnapshot
        A live child whose stdin still has a parent write end.
    """
    return _ChildStallSnapshot(
        ProcessState(pid, "S", exited=False, wchan="pipe_read", available=True),
        (ChildPipe(0, "pipe:[42]", None),),
        (10,),
        stage_index,
        None,
    )


def _runnable_child(
    *,
    pid: int = 4244,
    stage_index: int = 2,
) -> _ChildStallSnapshot:
    """Build a live child the kernel could run, with nothing holding it.

    Returns
    -------
    _ChildStallSnapshot
        A child in state ``R`` whose stdin has no parent writer and which has
        written no output — the shape a starved host leaves behind, where the
        pipeline was ready and nothing was blocking it.
    """
    return _ChildStallSnapshot(
        ProcessState(pid, "R", exited=False, wchan=None, available=True),
        (ChildPipe(0, "pipe:[44]", None),),
        (),
        stage_index,
        None,
    )


@dc.dataclass(frozen=True, slots=True)
class _Telemetry:
    """The external measurements that characterize a stall without deciding it.

    The debugging plan pairs these two by name: they describe the failure for a
    reader but never determine the verdict, which is why they travel together
    and neither is a classifier input.
    """

    fd_delta: int
    thread_delta: int


#: The deltas the fixtures below declare; a report may override them.
_TELEMETRY = _Telemetry(fd_delta=1, thread_delta=2)

#: The reason both snapshot fixtures record. The distinctive report below
#: deliberately records another one, so a formatter that hardcoded this text
#: could not satisfy the semantic assertions.
_REASON = "no observable progress"


def _report(
    *,
    verdict: HandOffVerdict,
    evidence: _StallSnapshot,
    attempt: int = 3,
    telemetry: _Telemetry = _TELEMETRY,
) -> _StallReport:
    """Build one classified stall report from fixed evidence.

    The evidence arrives as the snapshot it already is. ``children`` and
    ``task_names`` are that dataclass's first two fields, and a real attempt
    captures one and then pairs it with a verdict — so naming them separately
    here would describe one value twice, in a weaker form.

    ``pipeline`` is left ``None``, which the diagnostic renders as the
    ``task=None`` case the field exists to make explicit; a real pipeline's
    repr embeds absolute workspace paths that no snapshot should carry.

    Returns
    -------
    _StallReport
        A report whose every field is a value the caller declared.
    """
    return _StallReport(
        attempt=attempt,
        active_backend=None,
        pipeline=None,
        fd_delta=telemetry.fd_delta,
        thread_delta=telemetry.thread_delta,
        verdict=verdict,
        snapshot=evidence,
        progress=LivenessProgress(quiet_for_s=0.75, deadline_remaining_s=12.5),
    )


def test_the_diagnostic_renders_a_hung_hand_off_report(
    snapshot: SnapshotAssertion,
) -> None:
    """The message names the attempt, the pipe evidence, and the pending tasks."""
    report = _report(
        verdict=HandOffVerdict.HUNG_HANDOFF,
        evidence=_StallSnapshot(
            (_exited_child_with_parent_output(), _blocked_on_parent_stdin()),
            ("native-pipeline-hand-off:pipeline_wait",),
            _REASON,
        ),
    )

    message = _format_stall(report)

    assert message == snapshot, (
        "the rendered stall diagnostic is a contract: a CI failure is read "
        "from this text alone"
    )


def test_the_diagnostic_renders_a_host_starvation_report(
    snapshot: SnapshotAssertion,
) -> None:
    """A starvation report carries the same evidence under its own verdict."""
    report = _report(
        verdict=HandOffVerdict.HOST_STARVATION,
        evidence=_StallSnapshot((_runnable_child(),), (), _REASON),
    )

    assert _format_stall(report) == snapshot, (
        "the starvation variant must be snapshotted too, or half the "
        "diagnostic's variants go unpinned"
    )


def _distinctive_report() -> _StallReport:
    """Build a report whose every rendered field carries a chosen value.

    The attempt, both deltas, and the reason deliberately differ from the
    fixtures' defaults, so a rendering that hardcoded a default — or dropped a
    field and read a constant — could not satisfy the assertions below.

    Returns
    -------
    _StallReport
        A report whose attempt, deltas, verdict, reason, and evidence are all
        the values this module chose rather than any fixture default.
    """
    return _report(
        verdict=HandOffVerdict.HUNG_HANDOFF,
        evidence=_StallSnapshot(
            (_exited_child_with_parent_output(), _blocked_on_parent_stdin()),
            ("native-pipeline-hand-off:pipeline_wait",),
            "generator yielded no progress event",
        ),
        attempt=11,
        telemetry=_Telemetry(fd_delta=7, thread_delta=8),
    )


@pytest.mark.parametrize(
    "fragment",
    [
        pytest.param("attempt=11", id="attempt"),
        pytest.param("fd_delta=7", id="fd_delta"),
        pytest.param("thread_delta=8", id="thread_delta"),
        pytest.param("verdict=HUNG_HANDOFF", id="verdict"),
        pytest.param("reason='generator yielded no progress event'", id="reason"),
        pytest.param(
            "pending_tasks=('native-pipeline-hand-off:pipeline_wait',)",
            id="pending_tasks",
        ),
        pytest.param("stdout_parent=9:pipe:[43]:5", id="parent_output"),
        pytest.param("stdin_parent_writers=(10,)", id="parent_writers"),
    ],
)
def test_the_diagnostic_names_each_field_it_was_given(fragment: str) -> None:
    """Removing a named field from the message must fail a semantic assertion.

    The snapshots above pin the whole rendering. They are not sufficient on
    their own here: they keep passing when the named fields are replaced with
    constants, because the constants chosen for the fixtures are what the
    snapshot already records. This test probes each field with a value that
    differs from every fixture default, so dropping the field fails under the
    name of the evidence that went missing.

    One fragment per invocation keeps each field an independently reported
    case, so a failure names the contract that broke.

    ``reason`` is the case that proves the point: the two snapshots do record
    it, but they record the same text the fixtures declare, so a formatter
    printing a constant would satisfy both. This report carries other text.
    """
    message = _format_stall(_distinctive_report())

    assert fragment in message, (
        f"the diagnostic must carry {fragment!r}, got {message!r}"
    )
