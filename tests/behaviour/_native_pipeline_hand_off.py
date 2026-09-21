"""Stress-test support for Linux-native pipeline stream hand-offs."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import enum
import pathlib
import threading
import time
import typing as typ

import pytest

from cuprum import ECHO, ScopeConfig, TimeoutExpired, scoped, sh
from tests.helpers.catalogue import combine_programs_into_catalogue, python_catalogue
from tests.helpers.process_state import (
    ChildPipe,
    ProcessState,
    child_pipes,
    parent_write_fds,
    process_state,
)
from tests.helpers.timeouts import pending_tasks

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._backend import StreamBackend
    from cuprum.events import ExecEvent
    from cuprum.program import Program
    from cuprum.sh import Pipeline, PipelineResult


_REPEATED_NATIVE_PIPELINE_ATTEMPTS = 16
_REPEATED_NATIVE_PIPELINE_TIMEOUT_S = 30.0
_REPEATED_NATIVE_PIPELINE_FD_TOLERANCE = 4
_NO_PROGRESS_INTERVAL_S = 0.5
_PROGRESS_PROBE_INTERVAL_S = 0.05


def _open_fd_count() -> int:
    """Count the descriptors currently open in this process."""
    return sum(1 for _ in pathlib.Path("/proc/self/fd").iterdir())


class HandOffVerdict(enum.StrEnum):
    """The evidence-based outcome for a stalled native pipeline attempt."""

    HUNG_HANDOFF = "HUNG_HANDOFF"
    HOST_STARVATION = "HOST_STARVATION"


@dc.dataclass(slots=True)
class _ProgressTracker:
    """Track stage PIDs and the most recent observable pipeline activity."""

    pids: set[int] = dc.field(default_factory=set)
    last_progress_at: float = dc.field(default_factory=time.monotonic)

    def observe(self, event: ExecEvent) -> None:
        """Record lifecycle events that prove the pipeline is still progressing."""
        if event.phase not in {"start", "stdout", "stderr", "exit"}:
            return
        if event.phase == "start" and event.pid is not None:
            self.pids.add(event.pid)
        self.last_progress_at = time.monotonic()


@dc.dataclass(frozen=True, slots=True)
class _ChildStallSnapshot:
    """A child's live process and stdin-pipe evidence at a pipeline stall."""

    process: ProcessState
    pipes: tuple[ChildPipe, ...]
    stdin_parent_writers: tuple[int, ...]


@dc.dataclass(frozen=True, slots=True)
class _StallSnapshot:
    """All evidence captured before cancelling a stalled pipeline task."""

    children: tuple[_ChildStallSnapshot, ...]
    task_names: tuple[str, ...]
    reason: str


@dc.dataclass(frozen=True, slots=True)
class _StallReport:
    """A classified stall paired with the attempt it interrupted."""

    attempt: int
    active_backend: StreamBackend
    pipeline: Pipeline
    fd_delta: int
    thread_delta: int
    verdict: HandOffVerdict
    snapshot: _StallSnapshot


def _make_echo_python_pipeline(
    python_code: str,
) -> tuple[sh.Pipeline, frozenset[Program]]:
    """Build the shared echo-to-python pipeline for backend selection tests."""
    _, python_program = python_catalogue()
    catalogue = combine_programs_into_catalogue(
        ECHO,
        python_program,
        project_name="backend-pipeline-tests",
    )
    echo = sh.make(ECHO, catalogue=catalogue)
    python = sh.make(python_program, catalogue=catalogue)
    return (
        echo("-n", "hello") | python("-c", python_code),
        frozenset([ECHO, python_program]),
    )


def _snapshot_child(pid: int) -> _ChildStallSnapshot:
    """Capture the state and stdin writer ownership for one tracked child."""
    pipes = child_pipes(pid)
    stdin_pipe = next((pipe for pipe in pipes if pipe.fd == 0), None)
    writers = () if stdin_pipe is None else parent_write_fds(stdin_pipe.target)
    return _ChildStallSnapshot(process_state(pid), pipes, writers)


def _task_name(task: asyncio.Task[object]) -> str:
    """Render a pending task's stable name and coroutine qualifier."""
    coroutine = task.get_coro()
    coroutine_name = "unavailable" if coroutine is None else coroutine.__qualname__
    return f"{task.get_name()}:{coroutine_name}"


def _capture_stall(tracker: _ProgressTracker, reason: str) -> _StallSnapshot:
    """Collect discriminator evidence before cancellation changes child state."""
    return _StallSnapshot(
        children=tuple(_snapshot_child(pid) for pid in sorted(tracker.pids)),
        task_names=tuple(sorted(_task_name(task) for task in pending_tasks())),
        reason=reason,
    )


async def _monitor_progress(
    tracker: _ProgressTracker,
    aggregate_deadline: float,
) -> _StallSnapshot:
    """Wait until observation ceases or the suite-safety backstop is reached."""
    while True:
        now = time.monotonic()
        quiet_for_s = now - tracker.last_progress_at
        if quiet_for_s >= _NO_PROGRESS_INTERVAL_S:
            return _capture_stall(tracker, "no observable pipeline progress")
        if now >= aggregate_deadline:
            return _capture_stall(tracker, "aggregate suite-safety backstop")
        pause_s = min(
            _PROGRESS_PROBE_INTERVAL_S,
            _NO_PROGRESS_INTERVAL_S - quiet_for_s,
            aggregate_deadline - now,
        )
        await asyncio.sleep(max(0.0, pause_s))


async def _run_liveness_bounded(
    pipeline: Pipeline,
    tracker: _ProgressTracker,
    aggregate_deadline: float,
) -> PipelineResult | _StallSnapshot:
    """Run a pipeline until it settles or its observation stream goes quiet."""
    pipeline_task = asyncio.create_task(
        pipeline.run(timeout=None),
        name="native-pipeline-hand-off",
    )
    monitor_task = asyncio.create_task(
        _monitor_progress(tracker, aggregate_deadline),
        name="native-pipeline-progress-monitor",
    )
    done, _ = await asyncio.wait(
        (pipeline_task, monitor_task),
        return_when=asyncio.FIRST_COMPLETED,
    )
    if pipeline_task in done:
        monitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor_task
        return pipeline_task.result()
    snapshot = monitor_task.result()
    pipeline_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await pipeline_task
    return snapshot


def _is_runnable(child: _ChildStallSnapshot) -> bool:
    """Return whether a live child could be awaiting host CPU or I/O service."""
    process = child.process
    return process.available and not process.exited and process.state in {"R", "D"}


def _exited_children_left_output(snapshot: _StallSnapshot) -> bool:
    """Return whether exited children left output waiting in a pipe."""
    children = snapshot.children
    return (
        bool(children)
        and all(child.process.exited for child in children)
        and any(
            pipe.pending_bytes not in {None, 0}
            for child in children
            for pipe in child.pipes
        )
    )


def _quiet_tasks_have_no_runnable_child(snapshot: _StallSnapshot) -> bool:
    """Return whether pending tasks outlived every runnable child process."""
    if not snapshot.task_names or not snapshot.children:
        return False
    return not any(_is_runnable(child) for child in snapshot.children)


def _classify_stall(snapshot: _StallSnapshot) -> HandOffVerdict:
    """Classify a stall from child liveness and pipe-ownership evidence."""
    has_parent_writer_pipe_read = any(
        child.stdin_parent_writers and child.process.wchan == "pipe_read"
        for child in snapshot.children
    )
    if has_parent_writer_pipe_read:
        return HandOffVerdict.HUNG_HANDOFF
    if _exited_children_left_output(snapshot):
        return HandOffVerdict.HUNG_HANDOFF
    if _quiet_tasks_have_no_runnable_child(snapshot):
        return HandOffVerdict.HUNG_HANDOFF
    return HandOffVerdict.HOST_STARVATION


def _format_child(child: _ChildStallSnapshot) -> str:
    """Format all discriminator fields for one child process."""
    process = child.process
    pipe_details = (
        f"{pipe.fd}:{pipe.target}:{pipe.pending_bytes!r}" for pipe in child.pipes
    )
    pipes = ", ".join(pipe_details)
    return (
        f"pid={process.pid},state={process.state!r},wchan={process.wchan!r},"
        f"exited={process.exited},available={process.available},"
        f"stdin_parent_writers={child.stdin_parent_writers!r},pipes=[{pipes}]"
    )


def _format_stall(report: _StallReport) -> str:
    """Format the full evidence needed to diagnose a native hand-off stall."""
    children = "; ".join(_format_child(child) for child in report.snapshot.children)
    return (
        "AUTO native pipeline hand-off stalled without observable progress "
        f"(attempt={report.attempt}, backend={report.active_backend.value}, "
        f"fd_delta={report.fd_delta}, thread_delta={report.thread_delta}, "
        f"task={report.pipeline!r}, verdict={report.verdict.value}, "
        f"reason={report.snapshot.reason!r}, children=[{children}], "
        f"pending_tasks={report.snapshot.task_names!r})"
    )


_DEEP_NATIVE_PIPELINE_HOPS = 6
_DEEP_NATIVE_PIPELINE_BYTES = 4 * 1024 * 1024
_DEEP_NATIVE_PIPELINE_TIMEOUT_S = 20.0


def assert_deep_native_pipeline_completes(
    active_backend: StreamBackend,
    allowlist: frozenset[Program],
    *,
    python_builder: cabc.Callable[..., sh.SafeCmd],
    cat_builder: cabc.Callable[..., sh.SafeCmd],
) -> None:
    """Pump a payload through more concurrent native hops than the idle pool.

    Every hop of a pipeline blocks while the pipe below it is full, and only a
    later hop can drain that pipe. A worker pool that made a submission wait
    for a free worker therefore deadlocked this shape as soon as the payload
    outgrew the capacity the hops share, so the chain runs deeper than the
    idle retention limit on a payload far larger than a single pipe buffer.
    """
    payload = (
        "import sys; "
        f"sys.stdout.buffer.write(b'x' * {_DEEP_NATIVE_PIPELINE_BYTES}); "
        "sys.stdout.flush()"
    )
    commands = [python_builder("-c", payload)]
    commands.extend(cat_builder() for _ in range(_DEEP_NATIVE_PIPELINE_HOPS))
    pipeline = sh.Pipeline(tuple(commands))

    started_at = time.monotonic()
    try:
        with scoped(ScopeConfig(allowlist=allowlist)):
            result = pipeline.run_sync(timeout=_DEEP_NATIVE_PIPELINE_TIMEOUT_S)
    except TimeoutExpired as error:
        pytest.fail(
            "a native pipeline deeper than the idle worker pool must still "
            f"complete (hops={_DEEP_NATIVE_PIPELINE_HOPS}, "
            f"bytes={_DEEP_NATIVE_PIPELINE_BYTES}, "
            f"backend={active_backend.value}, "
            f"elapsed_s={time.monotonic() - started_at:.3f}, "
            f"error={error!r})",
        )
    stdout_text = result.stdout
    if stdout_text is None:
        pytest.fail(
            "the final stage of a capturing pipeline must return its stdout, "
            f"found {result.stdout!r}",
        )
    if len(stdout_text) != _DEEP_NATIVE_PIPELINE_BYTES:
        pytest.fail(
            "every byte must survive a deep native hand-off, found "
            f"{len(stdout_text)} of {_DEEP_NATIVE_PIPELINE_BYTES}",
        )
    if not result.ok:
        pytest.fail(
            "all stages of a deep native pipeline must exit successfully, "
            f"found failure_index={result.failure_index!r} with stage "
            f"exit codes "
            f"{[stage.exit_code for stage in result.stages]!r}",
        )


@dc.dataclass(frozen=True, slots=True)
class _NativePipelineHandOff:
    """Configuration and accounting for one repeated native hand-off run."""

    active_backend: StreamBackend
    make_pipeline: cabc.Callable[[str], tuple[Pipeline, frozenset[Program]]]
    aggregate_deadline: float
    initial_thread_count: int

    def run_attempt(self, attempt: int, fd_delta: int) -> None:
        """Run one attempt and apply its evidence-based stall verdict."""
        pipeline, allowlist = self.make_pipeline(
            "import sys; sys.stdout.write(sys.stdin.read().upper())",
        )
        tracker = _ProgressTracker()
        with scoped(ScopeConfig(allowlist=allowlist, observe_hooks=(tracker.observe,))):
            outcome = asyncio.run(
                _run_liveness_bounded(pipeline, tracker, self.aggregate_deadline),
            )
        thread_delta = threading.active_count() - self.initial_thread_count
        if isinstance(outcome, _StallSnapshot):
            report = _StallReport(
                attempt=attempt,
                active_backend=self.active_backend,
                pipeline=pipeline,
                fd_delta=fd_delta,
                thread_delta=thread_delta,
                verdict=_classify_stall(outcome),
                snapshot=outcome,
            )
            message = _format_stall(report)
            if report.verdict is HandOffVerdict.HUNG_HANDOFF:
                pytest.fail(message)
            pytest.skip(message)
        if outcome.stdout != "HELLO":
            pytest.fail(
                f"attempt {attempt} with backend={self.active_backend.value} lost "
                "pipeline output",
            )
        if not outcome.ok:
            pytest.fail(
                f"attempt {attempt} with backend={self.active_backend.value} had a "
                "non-zero stage",
            )


def assert_repeated_native_pipeline_hand_off(
    active_backend: StreamBackend,
    make_pipeline: cabc.Callable[[str], tuple[Pipeline, frozenset[Program]]],
) -> None:
    """Exercise repeated native hand-offs using progress as the liveness bound."""
    aggregate_deadline = time.monotonic() + _REPEATED_NATIVE_PIPELINE_TIMEOUT_S
    initial_fd_count = _open_fd_count()
    initial_thread_count = threading.active_count()
    hand_off = _NativePipelineHandOff(
        active_backend, make_pipeline, aggregate_deadline, initial_thread_count
    )
    for attempt in range(_REPEATED_NATIVE_PIPELINE_ATTEMPTS):
        fd_count = _open_fd_count()
        fd_delta = fd_count - initial_fd_count
        if fd_delta > _REPEATED_NATIVE_PIPELINE_FD_TOLERANCE:
            pytest.fail(
                "native pipeline hand-off leaked descriptors across attempts "
                f"(attempt={attempt}, initial_fd_count={initial_fd_count}, "
                f"fd_count={fd_count}, fd_delta={fd_delta})",
            )
        if time.monotonic() >= aggregate_deadline:
            pytest.skip(
                "host starvation consumed the native pipeline hand-off "
                f"suite-safety backstop before attempt {attempt} "
                f"(fd_delta={fd_delta}, thread_delta="
                f"{threading.active_count() - initial_thread_count})",
            )
        hand_off.run_attempt(attempt, fd_delta)
