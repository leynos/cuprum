"""Pipeline settlement follows the result of its after-hooks."""

from __future__ import annotations

import asyncio
import contextlib
import time
from functools import partial

import pytest

from cuprum import ECHO, sh
from cuprum._pipeline_internals import (
    _finalize_pipeline_execution,
    _finalize_pipeline_run_failure,
)
from cuprum._pipeline_types import (
    _EventDetails,
    _ExecutionHooks,
    _PipelineObservers,
    _PipelineSpawnResult,
    _StageObservation,
    _StageWaitContext,
)
from cuprum._sink_lifecycle import _SinkBracket
from cuprum._testing import _prepare_pipeline_config
from cuprum.events import ExecEvent, TerminalOutcome
from cuprum.sh import ExecutionContext, RunOutputOptions


class _AfterHookError(Exception):
    """Raised by the failing pipeline after-hook in this test."""


class _SettledObserveError(Exception):
    """Raised by the first stage's settled observer in this test."""


class _TerminalHookGate:
    """Hold terminal observers until the test delivers both cancellations."""

    def __init__(self, stage_count: int) -> None:
        self.stage_count = stage_count
        self.release = asyncio.Event()
        self.all_started = asyncio.Event()
        self.started_count = 0

    async def hold(self, event: ExecEvent) -> None:
        """Block each settled hook until the test releases the gate."""
        if event.phase != "settled":
            return
        self.started_count += 1
        if self.started_count == self.stage_count:
            self.all_started.set()
        await self.release.wait()


def _append_settlement(event: ExecEvent, *, events: list[ExecEvent]) -> None:
    """Record a terminal event for one pipeline stage."""
    if event.phase == "settled":
        events.append(event)


def _make_cancellable_stage_observations(
    command: sh.SafeCmd,
    gate: _TerminalHookGate,
) -> tuple[
    tuple[_StageObservation, ...], list[list[ExecEvent]], list[asyncio.Task[None]]
]:
    """Build two observed stages whose terminal hooks share one drain gate."""
    stage_count = 2
    settled_events: list[list[ExecEvent]] = [[] for _ in range(stage_count)]
    pending_tasks: list[asyncio.Task[None]] = []
    observations: list[_StageObservation] = []
    for stage_index, events in enumerate(settled_events):
        observation = _StageObservation(
            cmd=command,
            hooks=_ExecutionHooks(
                before_hooks=(),
                after_hooks=(),
                observe_hooks=(
                    partial(_append_settlement, events=events),
                    gate.hold,
                ),
            ),
            tags={
                "pipeline_stage_index": stage_index,
                "pipeline_stages": stage_count,
            },
            cwd=None,
            env_overlay=None,
            pending_tasks=pending_tasks,
            wall_clock=time.time,
        )
        observation.emit("plan", _EventDetails(pid=None))
        observations.append(observation)
    return tuple(observations), settled_events, pending_tasks


async def _cancel_finalization_twice_after_hooks_start(
    finalization: asyncio.Task[None],
    gate: _TerminalHookGate,
) -> None:
    """Deliver repeated cancellation while settlement observers are draining."""
    try:
        async with asyncio.timeout(1.0):
            await gate.all_started.wait()
        assert finalization.cancel(), "the first cancellation must reach finalization"
        await asyncio.sleep(0)
        assert finalization.cancel(), "the second cancellation must reach finalization"
        await asyncio.sleep(0)
    finally:
        gate.release.set()
        if not finalization.done():
            with contextlib.suppress(asyncio.CancelledError):
                await finalization


def _assert_every_stage_settled_once(
    settled_events: list[list[ExecEvent]],
) -> None:
    """Check one cancellation settlement was observed for each stage."""
    for stage_index, events in enumerate(settled_events):
        assert len(events) == 1, (
            f"pipeline stage {stage_index} must settle exactly once"
        )
        assert events[0].terminal_outcome is TerminalOutcome.CANCELLED, (
            f"pipeline stage {stage_index} must retain cancellation outcome"
        )


def test_after_hook_failure_settles_each_stage_as_error() -> None:
    """A successful child exit cannot hide a failed pipeline after-hook."""

    async def run() -> None:
        """Finalize one observed stage after its after-hook raises."""
        events: list[ExecEvent] = []
        command = sh.make(ECHO)("hello")

        def failing_after_hook(
            _command: sh.SafeCmd,
            _result: sh.CommandResult,
        ) -> None:
            """Fail after the child has produced a successful result."""
            raise _AfterHookError

        observation = _StageObservation(
            cmd=command,
            hooks=_ExecutionHooks(
                before_hooks=(),
                after_hooks=(failing_after_hook,),
                observe_hooks=(events.append,),
            ),
            tags={"pipeline_stage_index": 0, "pipeline_stages": 1},
            cwd=None,
            env_overlay=None,
            pending_tasks=[],
            wall_clock=time.time,
        )
        observation.emit("plan", _EventDetails(pid=None))
        observation.emit("start", _EventDetails(pid=123))
        observation.emit("exit", _EventDetails(pid=123, exit_code=0))
        result = sh.CommandResult(
            program=command.program,
            argv=command.argv,
            exit_code=0,
            pid=123,
            stdout=None,
            stderr=None,
            started_at=0.0,
            duration=0.0,
        )

        with pytest.raises(_AfterHookError):
            await _finalize_pipeline_execution(
                (command,),
                _PipelineObservers((observation,), []),
                [result],
                _SinkBracket(None),
            )

        settled = [event for event in events if event.phase == "settled"]
        assert len(settled) == 1, "the planned stage must settle exactly once"
        assert settled[0].terminal_outcome is TerminalOutcome.ERROR, (
            "an after-hook failure must determine the stage's terminal category"
        )
        assert settled[0].exit_code == 0, (
            "settlement must preserve the child's real exit status"
        )
        assert settled[0].exec_id == observation.exec_id, (
            "settlement must retain the stage's execution identity"
        )

    asyncio.run(run())


def test_settled_observe_failure_reaches_every_successful_pipeline_stage() -> None:
    """Fail closed only after every planned stage receives settlement."""

    async def run() -> None:
        """Finalize two successful stages with one failing observer."""
        command = sh.make(ECHO)("hello")
        commands = (command, command)
        events_by_stage: list[list[ExecEvent]] = []
        observations: list[_StageObservation] = []
        results: list[sh.CommandResult] = []

        def failing_settled_hook(event: ExecEvent) -> None:
            """Fail only at the terminal boundary for the first stage."""
            if event.phase == "settled":
                raise _SettledObserveError

        for stage_index in range(len(commands)):
            events: list[ExecEvent] = []
            events_by_stage.append(events)
            observe_hooks = (
                (failing_settled_hook, events.append)
                if stage_index == 0
                else (events.append,)
            )
            observation = _StageObservation(
                cmd=command,
                hooks=_ExecutionHooks(
                    before_hooks=(),
                    after_hooks=(),
                    observe_hooks=observe_hooks,
                ),
                tags={
                    "pipeline_stage_index": stage_index,
                    "pipeline_stages": len(commands),
                },
                cwd=None,
                env_overlay=None,
                pending_tasks=[],
                wall_clock=time.time,
            )
            observations.append(observation)
            observation.emit("plan", _EventDetails(pid=None))
            observation.emit("start", _EventDetails(pid=200 + stage_index))
            observation.emit("exit", _EventDetails(pid=200 + stage_index, exit_code=0))
            results.append(
                sh.CommandResult(
                    program=command.program,
                    argv=command.argv,
                    exit_code=0,
                    pid=200 + stage_index,
                    stdout=None,
                    stderr=None,
                    started_at=0.0,
                    duration=0.0,
                )
            )

        with pytest.raises(_SettledObserveError):
            await _finalize_pipeline_execution(
                commands,
                _PipelineObservers(tuple(observations), []),
                results,
                _SinkBracket(None),
            )

        for stage_index, events in enumerate(events_by_stage):
            settled = [event for event in events if event.phase == "settled"]
            assert len(settled) == 1, (
                f"pipeline stage {stage_index} must settle despite an earlier "
                "stage's observer failure"
            )
            assert settled[0].terminal_outcome is TerminalOutcome.EXIT_ZERO, (
                f"pipeline stage {stage_index} must retain its process outcome"
            )

    asyncio.run(run())


def test_repeated_cancellation_settles_every_pipeline_stage() -> None:
    """Repeated caller cancellation cannot interrupt stage settlement."""
    asyncio.run(_run_repeated_cancellation_scenario())


async def _run_repeated_cancellation_scenario() -> None:
    """Cancel pipeline finalization while all stages drain terminal hooks."""
    command = sh.make(ECHO)("hello")
    gate = _TerminalHookGate(stage_count=2)
    observations, settled_events, pending_tasks = _make_cancellable_stage_observations(
        command,
        gate,
    )
    config = _prepare_pipeline_config(
        output=RunOutputOptions(capture=False),
        timeout=None,
        context=ExecutionContext(),
    )
    spawn = _PipelineSpawnResult(
        processes=[],
        stderr_tasks=[],
        stdout_task=None,
        relay_diagnostics_by_stage=(),
        stages=_StageWaitContext(started_at=()),
    )
    finalization = asyncio.create_task(
        _finalize_pipeline_run_failure(
            config,
            spawn,
            _PipelineObservers(observations, pending_tasks),
            asyncio.CancelledError(),
        )
    )

    await _cancel_finalization_twice_after_hooks_start(finalization, gate)
    with pytest.raises(asyncio.CancelledError):
        await finalization
    _assert_every_stage_settled_once(settled_events)
