"""Pipeline terminal-event guarantees during repeated caller cancellation."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import typing as typ

import pytest

from cuprum import ScopeConfig, _pipeline_finalize, scoped, sh
from cuprum.events import ExecEvent, TerminalOutcome
from cuprum.sh import Pipeline, RunOutputOptions
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum._subprocess_wait_types import (
        _StreamConsumerTask,
        _StreamPayloadPair,
    )
    from cuprum.program import Program

# The pipeline's cleanup reads this name from the finalization module, which is
# where the run-failure reconciliation moved when it was split out of
# ``_pipeline_internals``; patching the old home would leave the real one in use.
_REAL_CANCEL_STREAM_TASKS = _pipeline_finalize._cancel_stream_tasks


class _CommandCleanupGate:
    """Coordinate entry into and release from command cleanup."""

    def __init__(self) -> None:
        """Create the cleanup-entry and release signals."""
        self.entered = asyncio.Event()
        self.release = asyncio.Event()


@dc.dataclass
class _PipelineCancellationScenario:
    """Coordinate process starts, cleanup gating, and observed events."""

    pipeline: Pipeline
    python_program: Program
    events: list[ExecEvent] = dc.field(default_factory=list)
    all_stages_started: asyncio.Event = dc.field(default_factory=asyncio.Event)
    cleanup_entered: asyncio.Event = dc.field(default_factory=asyncio.Event)
    release_cleanup: asyncio.Event = dc.field(default_factory=asyncio.Event)
    stage_start_count: int = 0

    def observe(self, event: ExecEvent) -> None:
        """Record lifecycle events and wait until both children have started."""
        self.events.append(event)
        if event.phase == "start":
            self.stage_start_count += 1
            if self.stage_start_count == 2:
                self.all_stages_started.set()

    async def cancel_stream_tasks(
        self,
        stderr_tasks: list[_StreamConsumerTask | None],
        stdout_task: _StreamConsumerTask | None,
    ) -> None:
        """Hold outer cleanup open while additional cancellations arrive."""
        self.cleanup_entered.set()
        await self.release_cleanup.wait()
        await _REAL_CANCEL_STREAM_TASKS(stderr_tasks, stdout_task)


@pytest.fixture
def cancellation_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> _PipelineCancellationScenario:
    """Build a pipeline and gate its stream cleanup for cancellation tests."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    pipeline: Pipeline = python("-c", "import time; time.sleep(30)") | python(
        "-c", "import time; time.sleep(30)"
    )
    scenario = _PipelineCancellationScenario(pipeline, python_program)
    monkeypatch.setattr(
        _pipeline_finalize, "_cancel_stream_tasks", scenario.cancel_stream_tasks
    )
    return scenario


async def _cancel_pipeline_repeatedly(
    scenario: _PipelineCancellationScenario,
) -> None:
    """Cancel a live pipeline repeatedly while its cleanup is held open."""
    task = asyncio.create_task(
        scenario.pipeline.run(output=RunOutputOptions(capture=False))
    )
    try:
        await asyncio.wait_for(scenario.all_stages_started.wait(), timeout=5)
        assert task.cancel(), "the initial cancellation must reach a live pipeline"
        await asyncio.wait_for(scenario.cleanup_entered.wait(), timeout=5)

        for _ in range(3):
            assert task.cancel(), "each repeated cancellation must reach cleanup"
            for _ in range(4):
                await asyncio.sleep(0)
            assert not task.done(), "repeated cancellation must not abandon cleanup"

        scenario.release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        scenario.release_cleanup.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), timeout=5
            )


def test_repeated_cancellation_emits_one_terminal_outcome_per_stage(
    cancellation_scenario: _PipelineCancellationScenario,
) -> None:
    """Settle every planned stage once when cancellation repeats in cleanup."""
    with (
        scoped(
            ScopeConfig(allowlist=frozenset([cancellation_scenario.python_program]))
        ),
        sh.observe(cancellation_scenario.observe),
    ):
        asyncio.run(_cancel_pipeline_repeatedly(cancellation_scenario))

    planned = [event for event in cancellation_scenario.events if event.phase == "plan"]
    settled = [
        event for event in cancellation_scenario.events if event.phase == "settled"
    ]
    assert len(planned) == len(settled) == 2, (
        "each planned stage must settle once, got "
        f"{[event.phase for event in cancellation_scenario.events]}"
    )
    assert [event.exec_id for event in settled] == [
        event.exec_id for event in planned
    ], "each stage's terminal event must retain its execution identity"
    assert all(
        event.terminal_outcome is TerminalOutcome.CANCELLED for event in settled
    ), "repeated cancellation must retain the cancellation category"


def test_repeated_cancellation_settles_one_command_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated cancellation during cleanup still settles the observed command."""
    from cuprum import _subprocess_wait

    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    command = python("-c", "import time; time.sleep(30)")
    gate = _CommandCleanupGate()
    started = asyncio.Event()
    events: list[ExecEvent] = []
    real_drain = _subprocess_wait._drain_stream_consumers

    def observe(event: ExecEvent) -> None:
        """Record lifecycle events and signal the spawned child."""
        events.append(event)
        if event.phase == "start":
            started.set()

    async def gated_drain(
        consumers: tuple[_StreamConsumerTask, _StreamConsumerTask],
        context: _subprocess_wait._DrainContext,
    ) -> _StreamPayloadPair:
        """Hold reconciliation open while repeated cancellation arrives."""
        gate.entered.set()
        await gate.release.wait()
        return await real_drain(consumers, context)

    monkeypatch.setattr(_subprocess_wait, "_drain_stream_consumers", gated_drain)

    async def run_case() -> None:
        """Cancel after start, then interrupt the gated cleanup repeatedly."""
        task = asyncio.create_task(command.run())
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            await asyncio.wait_for(gate.entered.wait(), timeout=5)
            for _ in range(3):
                task.cancel()
                for _ in range(4):
                    await asyncio.sleep(0)
                assert not task.done(), (
                    "repeated cancellation must not abandon child cleanup"
                )
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        finally:
            gate.release.set()
            if not task.done():
                task.cancel()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(task, return_exceptions=True), timeout=5
                )

    with (
        scoped(ScopeConfig(allowlist=frozenset([python_program]))),
        sh.observe(observe),
    ):
        asyncio.run(run_case())

    _assert_single_cancelled_settlement(events)


def _assert_single_cancelled_settlement(events: list[ExecEvent]) -> None:
    """Assert cancellation settles once without inventing child details."""
    planned = [event for event in events if event.phase == "plan"]
    settled = [event for event in events if event.phase == "settled"]
    phases = [event.phase for event in events]
    assert len(planned) == len(settled) == 1, (
        f"repeated cancellation must settle once, got {phases}"
    )
    assert settled[0].terminal_outcome is TerminalOutcome.CANCELLED, (
        "the run must retain its cancellation category"
    )
    assert settled[0].exec_id == planned[0].exec_id, (
        "the terminal event must retain the run identity"
    )
    assert settled[0].pid is None, "cancellation must not invent a PID"
    assert settled[0].exit_code is None, "cancellation must not invent a child status"
