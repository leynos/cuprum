"""Runtime coverage for CQRS hook and task-scheduling behaviour."""

from __future__ import annotations

import asyncio
import io
import itertools
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum import (
    LS,
    ForbiddenProgramError,
    ScopeConfig,
    before,
    scoped,
    sh,
)
from cuprum._observability import (
    _drain_tasks_during_cleanup,
    _emit_exec_event,
    _ExecEventEmissionError,
    _wait_for_exec_hook_tasks,
)
from cuprum._pipeline_internals import (
    _PIPELINE_FINALIZATION_ERROR,
    _finalize_pipeline_execution,
)
from cuprum._pipeline_types import (
    _EventDetails,
    _ExecutionHooks,
    _PipelineObservers,
    _StageObservation,
)
from cuprum._sink_lifecycle import _SinkBracket
from cuprum.sinks import (
    OutputSession,
    SessionOutcome,
    SessionStart,
    TerminalOutcome,
)
from cuprum.unittests._cqrs_fixtures import _echo_cmd, _event

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent


import time

"""Runtime coverage for CQRS hook and task-scheduling behaviour."""
if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent


class _GeneratedHookError(Exception):
    """Test exception raised by generated hook-ordering cases."""


class _AfterHookError(Exception):
    """Test exception raised by a pipeline after hook."""


class _ObserveTaskError(Exception):
    """Test exception raised by a scheduled observe task."""


class _FatalObserveTaskError(BaseException):
    """Test base exception raised by a scheduled observe task."""


class _OutcomeRecordingSession:
    """Minimal OutputSession recording the outcome it was closed with."""

    def __init__(self) -> None:
        """Start with an in-memory log and no closed outcome."""
        self.log_io = io.StringIO()
        self.closed_with: SessionOutcome | None = None

    @property
    def log(self) -> typ.IO[str]:
        """The in-memory log destination."""
        return self.log_io

    def close(self, outcome: SessionOutcome) -> None:
        """Record the terminal outcome."""
        self.closed_with = outcome


class _OutcomeRecordingSink:
    """Minimal OutputSink returning an outcome-recording session."""

    def __init__(self) -> None:
        """Start with no opened session."""
        self.session: _OutcomeRecordingSession | None = None

    def open_session(self, start: SessionStart) -> OutputSession | None:
        """Return a fresh recording session, ignoring the start."""
        del start
        self.session = _OutcomeRecordingSession()
        return self.session


def _open_bracket() -> tuple[_SinkBracket, _OutcomeRecordingSession]:
    """Open a bracket over a recording sink and return it with its session."""
    sink = _OutcomeRecordingSink()
    bracket = _SinkBracket.open(sink, SessionStart(label="project: program", argv=()))
    session = sink.session
    assert session is not None, "a recording sink must return an active session"
    return bracket, session


async def _finalize_with_failing_after_hook(
    pending_tasks: list[asyncio.Task[None]],
    observe_task_factory: cabc.Callable[[], cabc.Awaitable[None]],
    sink_bracket: _SinkBracket,
) -> None:
    """Finalize a pipeline with a failing after hook and an observe task.

    The bracket is supplied by the caller so a test can inspect the outcome
    the failing hook commits, which is the behaviour the ordering protects.
    """

    async def await_observe_task() -> None:
        """Adapt the supplied awaitable for ``asyncio.create_task``."""
        await observe_task_factory()

    def failing_after_hook(_cmd: sh.SafeCmd, _result: sh.CommandResult) -> None:
        """Raise while finalizing the pipeline stage."""
        raise _AfterHookError

    cmd = _echo_cmd()
    observation = _StageObservation(
        cmd=cmd,
        hooks=_ExecutionHooks(
            before_hooks=(),
            after_hooks=(failing_after_hook,),
            observe_hooks=(),
        ),
        tags={},
        cwd=None,
        env_overlay=None,
        pending_tasks=pending_tasks,
        wall_clock=time.time,
    )
    pending_tasks.append(asyncio.create_task(await_observe_task()))
    stage_result = sh.CommandResult(
        program=cmd.program,
        argv=cmd.argv,
        exit_code=0,
        pid=123,
        stdout=None,
        stderr=None,
        started_at=0.0,
        duration=0.0,
    )
    await _finalize_pipeline_execution(
        (cmd,),
        _PipelineObservers((observation,), pending_tasks),
        [stage_result],
        sink_bracket,
    )


async def _assert_pipeline_finalization_failure_group(
    task_exception_class: type[BaseException],
    expected_group_class: type[BaseExceptionGroup[BaseException]],
) -> None:
    """Assert finalization groups the after-hook and observe-task failures."""
    pending_tasks: list[asyncio.Task[None]] = []
    bracket, session = _open_bracket()

    async def failing_observe_task() -> None:
        """Yield once, then raise the scenario's observe-task exception."""
        await asyncio.sleep(0)
        raise task_exception_class

    with pytest.raises(expected_group_class) as exc_info:
        await _finalize_with_failing_after_hook(
            pending_tasks,
            failing_observe_task,
            bracket,
        )

    assert type(exc_info.value) is expected_group_class, (
        "finalization should raise the expected concrete exception-group type"
    )
    assert tuple(type(error) for error in exc_info.value.exceptions) == (
        _AfterHookError,
        task_exception_class,
    ), "finalization should preserve both failures in operation order"
    assert not pending_tasks, "finalization should clear failed observe tasks"
    # The hook failure is the run's terminal error, so it is what the adapter
    # must record: closing with the stage-result outcome first would annotate
    # a crashed run as an ordinary non-zero exit.
    assert session.closed_with == SessionOutcome(TerminalOutcome.ERROR), (
        f"a failing after-hook must close the sink as an error; "
        f"got {session.closed_with!r}"
    )


def test_safe_cmd_run_enforces_allowlist_before_before_hooks() -> None:
    """A forbidden ``SafeCmd.run_sync`` must not dispatch before hooks."""
    calls: list[sh.SafeCmd] = []

    def track_before(cmd: sh.SafeCmd) -> None:
        """Record whether a before hook ran."""
        calls.append(cmd)

    with (
        scoped(ScopeConfig(allowlist=frozenset([LS]))),
        before(track_before),
        pytest.raises(ForbiddenProgramError),
    ):
        _echo_cmd().run_sync()

    assert not calls, "before hooks should not run before allowlist enforcement"


@settings(deadline=None)
@given(
    hook_kinds=st.lists(
        st.sampled_from(("sync", "async", "fail")),
        min_size=1,
        max_size=8,
    )
)
def test_emit_exec_event_preserves_scheduled_prefix_for_generated_hook_orders(
    hook_kinds: list[str],
) -> None:
    """Generated hook orderings preserve tasks scheduled before failure."""

    async def run() -> None:
        """Drive generated hooks inside a running loop."""
        hooks = tuple(_generated_observe_hook(kind) for kind in hook_kinds)
        first_failure_index = _first_failure_index(hook_kinds)
        expected_scheduled = hook_kinds[
            : first_failure_index if first_failure_index is not None else None
        ].count("async")

        if first_failure_index is None:
            scheduled = _emit_exec_event(hooks, _event())
            assert len(scheduled) == expected_scheduled, (
                "all generated async hooks should be scheduled when no hook fails"
            )
            await asyncio.gather(*scheduled)
            return

        with pytest.raises(_ExecEventEmissionError) as exc_info:
            _emit_exec_event(hooks, _event())
        assert len(exc_info.value.scheduled_tasks) == expected_scheduled, (
            "failing hook emission should carry tasks scheduled before failure"
        )
        await asyncio.gather(*exc_info.value.scheduled_tasks)

    asyncio.run(run())


def test_pipeline_finalization_drains_tasks_after_hook_failure() -> None:
    """Finalization awaits observe tasks when a pipeline after hook raises."""

    async def run() -> None:
        """Run a failing after hook beside a scheduled observe task."""
        completed: list[bool] = []
        pending_tasks: list[asyncio.Task[None]] = []
        bracket, _session = _open_bracket()

        async def observe_task() -> None:
            """Record that finalization awaited the scheduled task."""
            await asyncio.sleep(0)
            completed.append(True)

        with pytest.raises(_AfterHookError):
            await _finalize_with_failing_after_hook(
                pending_tasks,
                observe_task,
                bracket,
            )

        assert completed == [True], "finalization should await scheduled observe tasks"
        assert not pending_tasks, "finalization should clear completed observe tasks"

    asyncio.run(run())


def test_pipeline_finalization_preserves_after_hook_and_task_failures() -> None:
    """Finalization groups simultaneous after-hook and observe-task failures."""
    asyncio.run(
        _assert_pipeline_finalization_failure_group(
            _ObserveTaskError,
            ExceptionGroup,
        )
    )


def test_pipeline_finalization_preserves_after_hook_and_base_exception_task() -> None:
    """Finalization groups an after-hook failure with a non-Exception task failure."""
    asyncio.run(
        _assert_pipeline_finalization_failure_group(
            _FatalObserveTaskError,
            BaseExceptionGroup,
        )
    )


def test_pipeline_cleanup_drain_failure_preserves_active_error() -> None:
    """Cleanup aggregates an observe-task failure with the active timeout error."""

    async def run() -> None:
        """Fail an observe task while a TimeoutExpired cleanup is active."""
        pending_tasks: list[asyncio.Task[None]] = []

        async def failing_observe_task() -> None:
            """Yield once, then fail while cleanup drains observe tasks."""
            await asyncio.sleep(0)
            raise _ObserveTaskError

        pending_tasks.append(asyncio.create_task(failing_observe_task()))
        timeout_error = sh.TimeoutExpired(cmd=("echo",), timeout=0.2)

        with pytest.raises(BaseExceptionGroup) as exc_info:
            await _drain_tasks_during_cleanup(
                pending_tasks,
                timeout_error,
                message=_PIPELINE_FINALIZATION_ERROR,
            )

        assert exc_info.value.message == _PIPELINE_FINALIZATION_ERROR, (
            "cleanup should label the group with the finalization message it was "
            f"given, got {exc_info.value.message!r}"
        )
        assert exc_info.value.exceptions[0] is timeout_error, (
            "cleanup should preserve the active error, not replace it with the drain "
            "failure"
        )
        assert tuple(type(error) for error in exc_info.value.exceptions) == (
            sh.TimeoutExpired,
            _ObserveTaskError,
        ), "cleanup should aggregate the active error with the observe-task failure"
        assert not pending_tasks, "cleanup should clear drained observe tasks"

    asyncio.run(run())


def _first_failure_index(hook_kinds: list[str]) -> int | None:
    """Return the index of the first generated failing hook."""
    try:
        return hook_kinds.index("fail")
    except ValueError:
        return None


def _generated_observe_hook(
    kind: str,
) -> cabc.Callable[[ExecEvent], cabc.Awaitable[None] | None]:
    """Build a generated observe hook of the requested kind."""

    async def async_hook(_event: ExecEvent) -> None:
        """Cross an async boundary for generated hook scheduling."""
        await asyncio.sleep(0)

    def sync_hook(event: ExecEvent) -> None:
        """Run a generated synchronous hook."""
        _ = event.phase

    def fail_hook(_event: ExecEvent) -> None:
        """Raise from a generated hook."""
        raise _GeneratedHookError

    match kind:
        case "async":
            return async_hook
        case "sync":
            return sync_hook
        case "fail":
            return fail_hook
        case _:
            msg = "unknown generated hook kind"
            raise ValueError(msg)


def test_stage_observations_share_pending_tasks_under_concurrent_emits() -> None:
    """Concurrent stage emissions append to the shared pending-task list."""

    async def run() -> None:
        """Emit from multiple stage observations in one event loop."""
        pending_tasks: list[asyncio.Task[None]] = []
        completed: list[int] = []
        observations = _stage_observations(pending_tasks, completed)

        await asyncio.gather(
            *itertools.starmap(_emit_after_yield, enumerate(observations))
        )

        assert len(pending_tasks) == len(observations), (
            "each concurrent stage emission should append one pending task"
        )
        await _wait_for_exec_hook_tasks(pending_tasks)
        assert sorted(completed) == list(range(len(observations))), (
            "all scheduled stage observe tasks should complete exactly once"
        )

    asyncio.run(run())


def _stage_observations(
    pending_tasks: list[asyncio.Task[None]],
    completed: list[int],
) -> tuple[_StageObservation, ...]:
    """Build observations sharing one pending-task collection."""

    async def async_hook(event: ExecEvent) -> None:
        """Record the stage index after an async scheduling boundary."""
        await asyncio.sleep(0)
        completed.append(typ.cast("int", event.tags["pipeline_stage_index"]))

    return tuple(
        _StageObservation(
            cmd=_echo_cmd(),
            hooks=_ExecutionHooks(
                before_hooks=(),
                after_hooks=(),
                observe_hooks=(async_hook,),
            ),
            tags={"pipeline_stage_index": idx},
            cwd=None,
            env_overlay=None,
            pending_tasks=pending_tasks,
            wall_clock=time.time,
        )
        for idx in range(4)
    )


async def _emit_after_yield(idx: int, observation: _StageObservation) -> None:
    """Yield once, then emit on the active event loop."""
    await asyncio.sleep(0)
    observation.emit("start", _EventDetails(pid=idx))
