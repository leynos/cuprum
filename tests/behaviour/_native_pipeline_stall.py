"""Stall evidence, verdicts, and classification for native pipeline hand-offs.

This module holds the pure diagnostic model. The liveness runner that produces
a stall snapshot lives in :mod:`tests.behaviour._native_pipeline_hand_off`; the
split keeps both modules inside the Pylint module-length limit.
"""

from __future__ import annotations

import dataclasses as dc
import enum
import typing as typ

if typ.TYPE_CHECKING:
    from cuprum._backend import StreamBackend
    from cuprum.sh import Pipeline
    from tests.helpers.process_state import ChildPipe, ProcessState


class HandOffVerdict(enum.StrEnum):
    """The evidence-based outcome for a stalled native pipeline attempt."""

    HUNG_HANDOFF = "HUNG_HANDOFF"
    HOST_STARVATION = "HOST_STARVATION"


@dc.dataclass(frozen=True, slots=True)
class _ParentOutput:
    """A parent-owned read end that drains a child's output pipe.

    The parent keeps these open for the lifetime of the run, so they remain
    observable after the writing child has exited and its own descriptors have
    vanished from procfs.
    """

    fd: int
    target: str
    pending_bytes: int | None


@dc.dataclass(frozen=True, slots=True)
class _ChildStallSnapshot:
    """A child's live process and pipe evidence at a pipeline stall."""

    process: ProcessState
    pipes: tuple[ChildPipe, ...]
    stdin_parent_writers: tuple[int, ...]
    stage_index: int | None
    stdout_parent: _ParentOutput | None


@dc.dataclass(frozen=True, slots=True)
class _StallSnapshot:
    """All evidence captured before cancelling a stalled pipeline task."""

    children: tuple[_ChildStallSnapshot, ...]
    task_names: tuple[str, ...]
    reason: str


@dc.dataclass(frozen=True, slots=True)
class _LivenessProgress:
    """Where the progress clock stood when a stall was captured.

    The liveness policy is otherwise unobservable from outside the runner: the
    snapshot says what the children were doing, and these two numbers say how
    long the observation stream had been quiet and how much of the suite-safety
    backstop had been consumed.
    """

    quiet_for_s: float
    deadline_remaining_s: float


@dc.dataclass(frozen=True, slots=True)
class _StallReport:
    """A classified stall paired with the attempt it interrupted.

    ``active_backend`` and ``pipeline`` are informational: they are rendered
    into the diagnostic message only, so an unclassified backstop path may
    leave them unset.
    """

    attempt: int
    active_backend: StreamBackend | None
    pipeline: Pipeline | None
    fd_delta: int
    thread_delta: int
    verdict: HandOffVerdict
    snapshot: _StallSnapshot
    progress: _LivenessProgress


def _is_runnable(child: _ChildStallSnapshot) -> bool:
    """Return whether a live child could be awaiting host CPU or I/O service."""
    process = child.process
    return process.available and not process.exited and process.state in {"R", "D"}


def _upstream_exited(
    child: _ChildStallSnapshot,
    children: tuple[_ChildStallSnapshot, ...],
) -> bool:
    """Report whether the stage feeding this child has already exited.

    ``pipeline_stage_index`` is the tag Cuprum puts on every pipeline event, so
    a child's position is known even though the typed ``ExecEvent.stage_index``
    field is set only on ``pipeline_fail_fast``.

    The upstream stage's *process* state is consulted rather than its ``exit``
    event: a pipeline emits ``exit`` for every stage only once the whole run has
    settled, so a stalled run never emits one, and gating on that event could
    not fire in the situation this rule exists to catch. A zombie and a reaped
    pid both report ``exited``, which is what matters here — either way the
    upstream process can no longer legitimately hold its write end open.

    An unknown position cannot be attributed to a specific edge, so it is not
    treated as exited.

    Returns
    -------
    bool
        ``True`` when the immediately preceding stage is present and exited.
    """
    if child.stage_index is None:
        return False
    upstream = child.stage_index - 1
    return any(
        other.stage_index == upstream and other.process.exited for other in children
    )


def _is_missed_hand_off(
    child: _ChildStallSnapshot,
    children: tuple[_ChildStallSnapshot, ...],
) -> bool:
    """Report whether a parent writer still feeds a blocked downstream reader.

    Only the edge this child actually reads from can prove a missed close. An
    unrelated runnable stage must not be able to hide a genuine hand-off
    failure, so this deliberately does not consult global child liveness.

    Returns
    -------
    bool
        ``True`` when this child waits in ``pipe_read`` on a parent-held write
        end whose upstream stage has already exited.
    """
    return (
        bool(child.stdin_parent_writers)
        and child.process.wchan == "pipe_read"
        and _upstream_exited(child, children)
    )


def _has_uncollected_output(child: _ChildStallSnapshot) -> bool:
    """Report whether a parent read end still holds output a child wrote.

    The child's own descriptors are not consulted: an exited child may be an
    unreaped zombie with no readable procfs entry, or already reaped, in which
    case its ``/proc`` directory is gone entirely.

    Returns
    -------
    bool
        ``True`` when the parent's draining read end reports queued bytes.
    """
    parent = child.stdout_parent
    return parent is not None and parent.pending_bytes not in {None, 0}


def _exited_children_left_output(snapshot: _StallSnapshot) -> bool:
    """Return whether exited children left output waiting uncollected."""
    children = snapshot.children
    return (
        bool(children)
        and all(child.process.exited for child in children)
        and any(_has_uncollected_output(child) for child in children)
    )


def _quiet_tasks_have_no_runnable_child(snapshot: _StallSnapshot) -> bool:
    """Return whether pending tasks outlived every runnable child process."""
    if not snapshot.task_names or not snapshot.children:
        return False
    return not any(_is_runnable(child) for child in snapshot.children)


def _classify_stall(snapshot: _StallSnapshot) -> HandOffVerdict:
    """Classify a stall from child liveness and pipe-ownership evidence.

    Every ``HUNG_HANDOFF`` rule is tested before the host-starvation fallback,
    so starvation is only ever reported when no positive evidence of a missed
    hand-off was captured.

    Returns
    -------
    HandOffVerdict
        ``HUNG_HANDOFF`` when any rule matched, otherwise ``HOST_STARVATION``.
    """
    if any(
        _is_missed_hand_off(child, snapshot.children) for child in snapshot.children
    ):
        return HandOffVerdict.HUNG_HANDOFF
    if _exited_children_left_output(snapshot):
        return HandOffVerdict.HUNG_HANDOFF
    if _quiet_tasks_have_no_runnable_child(snapshot):
        return HandOffVerdict.HUNG_HANDOFF
    return HandOffVerdict.HOST_STARVATION


def _describe_parent_output(parent: _ParentOutput | None) -> str:
    """Render the parent's draining read end, or its absence."""
    if parent is None:
        return "none"
    return f"{parent.fd}:{parent.target}:{parent.pending_bytes!r}"


def _format_child(child: _ChildStallSnapshot) -> str:
    """Format all discriminator fields for one child process."""
    process = child.process
    pipe_details = (
        f"{pipe.fd}:{pipe.target}:{pipe.pending_bytes!r}" for pipe in child.pipes
    )
    pipes = ", ".join(pipe_details)
    return (
        f"pid={process.pid},stage={child.stage_index!r},"
        f"state={process.state!r},wchan={process.wchan!r},"
        f"exited={process.exited},available={process.available},"
        f"stdin_parent_writers={child.stdin_parent_writers!r},"
        f"stdout_parent={_describe_parent_output(child.stdout_parent)},"
        f"pipes=[{pipes}]"
    )


def _format_stall(report: _StallReport) -> str:
    """Format the full evidence needed to diagnose a native hand-off stall."""
    children = "; ".join(_format_child(child) for child in report.snapshot.children)
    backend = (
        "unknown" if report.active_backend is None else report.active_backend.value
    )
    return (
        "AUTO native pipeline hand-off stalled without observable progress "
        f"(attempt={report.attempt}, backend={backend}, "
        f"fd_delta={report.fd_delta}, thread_delta={report.thread_delta}, "
        f"task={report.pipeline!r}, verdict={report.verdict.value}, "
        f"reason={report.snapshot.reason!r}, "
        f"quiet_for_s={report.progress.quiet_for_s:.3f}, "
        f"deadline_remaining_s={report.progress.deadline_remaining_s:.3f}, "
        f"children=[{children}], "
        f"pending_tasks={report.snapshot.task_names!r})"
    )
