"""Stress-test support for Linux-native pipeline stream hand-offs."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import pathlib
import threading
import time
import typing as typ

import pytest

from cuprum import ECHO, ScopeConfig, TimeoutExpired, scoped, sh
from tests.behaviour._native_pipeline_liveness import (
    PROGRESS_PHASES,
    STAGE_INDEX_TAG,
    LivenessBound,
)
from tests.behaviour._native_pipeline_stall import (
    HandOffVerdict,
    _ChildStallSnapshot,
    _classify_stall,
    _format_stall,
    _ParentOutput,
    _StallReport,
    _StallSnapshot,
)
from tests.helpers.catalogue import combine_programs_into_catalogue, python_catalogue
from tests.helpers.process_state import (
    child_pipes,
    parent_read_fds,
    parent_write_fds,
    process_state,
    read_end_pending_bytes,
)
from tests.helpers.timeouts import pending_tasks

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._backend import StreamBackend
    from cuprum.events import ExecEvent
    from cuprum.program import Program
    from cuprum.sh import Pipeline, PipelineResult

__all__ = [
    "HandOffVerdict",
    "LivenessBound",
    "_ChildStallSnapshot",
    "_StallSnapshot",
    "_classify_stall",
    "assert_deep_native_pipeline_completes",
    "assert_repeated_native_pipeline_hand_off",
]


_REPEATED_NATIVE_PIPELINE_ATTEMPTS = 16
_REPEATED_NATIVE_PIPELINE_TIMEOUT_S = 30.0
_REPEATED_NATIVE_PIPELINE_FD_TOLERANCE = 4


def _open_fd_count() -> int:
    """Count the descriptors currently open in this process.

    Returns
    -------
    int
        The number of entries in this process's descriptor directory.
    """
    return sum(1 for _ in pathlib.Path("/proc/self/fd").iterdir())


def _loop_int_tag(event: ExecEvent, key: str) -> int | None:
    """Read an integer position out of an event's ``tags`` mapping.

    Returns
    -------
    int | None
        The tagged position, or ``None`` when it is absent or not an integer.
    """
    value = event.tags.get(key)
    return value if isinstance(value, int) else None


@dc.dataclass(slots=True)
class _ProgressTracker:
    """Track stage positions and the most recent observable pipeline activity."""

    pids: set[int] = dc.field(default_factory=set)
    stage_by_pid: dict[int, int] = dc.field(default_factory=dict)
    stdout_target_by_pid: dict[int, str] = dc.field(default_factory=dict)
    last_progress_at: float = dc.field(default_factory=time.monotonic)

    def observe(self, event: ExecEvent) -> None:
        """Record lifecycle events that prove the pipeline is still progressing."""
        if event.phase not in PROGRESS_PHASES:
            return
        if event.phase == "start" and event.pid is not None:
            self.pids.add(event.pid)
            stage = _loop_int_tag(event, STAGE_INDEX_TAG)
            if stage is not None:
                self.stage_by_pid[event.pid] = stage
            self._remember_stdout_target(event.pid)
        self.last_progress_at = time.monotonic()

    def _remember_stdout_target(self, pid: int) -> None:
        """Record a child's stdout pipe name while the child can still be read.

        The name is captured on ``start`` because it is only observable for as
        long as the child is alive: the kernel releases a process's descriptors
        at exit, so ``/proc/<pid>/fd`` is already empty by the time a zombie can
        be sampled. The parent's matching read end outlives the child, and the
        pipe name is what lets the stall capture find it again.
        """
        target = next(
            (pipe.target for pipe in child_pipes(pid) if pipe.fd == 1),
            None,
        )
        if target is not None:
            self.stdout_target_by_pid[pid] = target


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


def _stdout_parent_output(target: str | None) -> _ParentOutput | None:
    """Find the parent read end that drains a child's stdout pipe.

    The pipe name is supplied by the tracker, which records it while the child
    is still alive: the child's own descriptors vanish at exit, so a stall
    capture that looked them up here would find nothing exactly when it needs
    them. Only the parent's end survives that transition.

    Returns
    -------
    _ParentOutput | None
        The parent's read end and its queued bytes, or ``None`` when no name
        was recorded or this process no longer holds a readable end.
    """
    if target is None:
        return None
    read_fds = parent_read_fds(target)
    if not read_fds:
        return None
    reader = read_fds[0]
    return _ParentOutput(reader, target, read_end_pending_bytes(reader))


def _snapshot_child(pid: int, tracker: _ProgressTracker) -> _ChildStallSnapshot:
    """Capture the state and pipe ownership for one tracked child."""
    pipes = child_pipes(pid)
    stdin_pipe = next((pipe for pipe in pipes if pipe.fd == 0), None)
    writers = () if stdin_pipe is None else parent_write_fds(stdin_pipe.target)
    return _ChildStallSnapshot(
        process_state(pid),
        pipes,
        writers,
        tracker.stage_by_pid.get(pid),
        _stdout_parent_output(tracker.stdout_target_by_pid.get(pid)),
    )


def _task_name(task: asyncio.Task[object]) -> str:
    """Render a pending task's stable name and coroutine qualifier."""
    coroutine = task.get_coro()
    coroutine_name = "unavailable" if coroutine is None else coroutine.__qualname__
    return f"{task.get_name()}:{coroutine_name}"


def _capture_stall(tracker: _ProgressTracker, reason: str) -> _StallSnapshot:
    """Collect discriminator evidence before cancellation changes child state."""
    return _StallSnapshot(
        children=tuple(_snapshot_child(pid, tracker) for pid in sorted(tracker.pids)),
        task_names=tuple(sorted(_task_name(task) for task in pending_tasks())),
        reason=reason,
    )


async def _monitor_progress(
    tracker: _ProgressTracker,
    bound: LivenessBound,
) -> _StallSnapshot:
    """Wait until observation ceases or the suite-safety backstop is reached.

    The progress it watches is the same progress the test consumes: every
    sample reads the clock the public ``ExecEvent`` observation seam advances,
    rather than a parallel notion of liveness maintained beside it.

    Returns
    -------
    _StallSnapshot
        The evidence captured under whichever bound was reached.
    """
    while True:
        if bound.settled(tracker.last_progress_at):
            return _capture_stall(tracker, "no observable pipeline progress")
        if bound.expired(tracker.last_progress_at):
            return _capture_stall(tracker, "aggregate suite-safety backstop")
        await asyncio.sleep(bound.pause_s(tracker.last_progress_at))


async def _run_liveness_bounded(
    pipeline: Pipeline,
    tracker: _ProgressTracker,
    bound: LivenessBound,
) -> PipelineResult | _StallSnapshot:
    """Run a pipeline until it settles or its observation stream goes quiet."""
    pipeline_task = asyncio.create_task(
        pipeline.run(timeout=None),
        name="native-pipeline-hand-off",
    )
    monitor_task = asyncio.create_task(
        _monitor_progress(tracker, bound),
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
    bound: LivenessBound
    initial_thread_count: int

    def run_attempt(self, attempt: int, fd_delta: int) -> None:
        """Run one attempt and apply its evidence-based stall verdict."""
        pipeline, allowlist = self.make_pipeline(
            "import sys; sys.stdout.write(sys.stdin.read().upper())",
        )
        tracker = _ProgressTracker()
        with scoped(ScopeConfig(allowlist=allowlist, observe_hooks=(tracker.observe,))):
            outcome = asyncio.run(
                _run_liveness_bounded(pipeline, tracker, self.bound),
            )
        thread_delta = threading.active_count() - self.initial_thread_count
        if isinstance(outcome, _StallSnapshot):
            # The default liveness policy runs this as a real test and expects
            # nothing to fail: a stall is the whole reason the support exists.
            report = _StallReport(
                attempt=attempt,
                active_backend=self.active_backend,
                pipeline=pipeline,
                fd_delta=fd_delta,
                thread_delta=thread_delta,
                verdict=_classify_stall(outcome),
                snapshot=outcome,
                progress=self.bound.progress(tracker.last_progress_at),
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
    """Exercise repeated native hand-offs using progress as the liveness bound.

    The liveness bound here is the shipped policy: the constants the runner
    declares, not a test-local copy of them.
    """
    hand_off = _NativePipelineHandOff(
        active_backend,
        make_pipeline,
        LivenessBound(
            aggregate_deadline=time.monotonic() + _REPEATED_NATIVE_PIPELINE_TIMEOUT_S
        ),
        threading.active_count(),
    )
    initial_fd_count = _open_fd_count()
    for attempt in range(_REPEATED_NATIVE_PIPELINE_ATTEMPTS):
        fd_count = _open_fd_count()
        fd_delta = fd_count - initial_fd_count
        if fd_delta > _REPEATED_NATIVE_PIPELINE_FD_TOLERANCE:
            pytest.fail(
                "native pipeline hand-off leaked descriptors across attempts "
                f"(attempt={attempt}, initial_fd_count={initial_fd_count}, "
                f"fd_count={fd_count}, fd_delta={fd_delta})",
            )
        if hand_off.bound.pre_attempt_backstop_reached():
            pytest.skip(
                "the native pipeline hand-off suite-safety backstop expired "
                f"before attempt {attempt} could start; this attempt is "
                "unclassified because no child state is observable yet "
                f"(fd_delta={fd_delta}, thread_delta="
                f"{threading.active_count() - hand_off.initial_thread_count})",
            )
        hand_off.run_attempt(attempt, fd_delta)
