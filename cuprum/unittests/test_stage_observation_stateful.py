"""Model pipeline settlement through observation and finalization paths."""

from __future__ import annotations

import asyncio
import time
import typing as typ

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from cuprum import sh
from cuprum._pipeline_internals import _finalize_pipeline_execution
from cuprum._pipeline_types import (
    _EventDetails,
    _ExecutionHooks,
    _PipelineObservers,
    _StageObservation,
)
from cuprum._sink_lifecycle import _SinkBracket
from cuprum.events import ExecEvent, TerminalOutcome
from cuprum.unittests._cqrs_fixtures import _echo_cmd

if typ.TYPE_CHECKING:
    from cuprum.sh import SafeCmd

_MAX_STAGE_COUNT = 5


class _SettlementStateMachine(RuleBasedStateMachine):
    """Generate stage counts and lifecycle orderings for a pipeline."""

    def __init__(self) -> None:
        """Create empty model state before Hypothesis chooses pipeline size."""
        super().__init__()
        self.stage_count = 0
        self.commands: list[SafeCmd] = []
        self.observations: list[_StageObservation] = []
        self.events_by_stage: list[list[ExecEvent]] = []
        self.planned: list[bool] = []
        self.started: list[bool] = []
        self.exited: list[bool] = []
        self.started_pids: list[int | None] = []
        self.exit_codes: list[int | None] = []
        self.expected_settlements: list[
            tuple[TerminalOutcome, int | None, int | None] | None
        ] = []
        self.pending_tasks: list[asyncio.Task[None]] = []
        self.completed = False

    @initialize(
        stage_count=st.integers(min_value=2, max_value=_MAX_STAGE_COUNT),
        planned_count=st.integers(min_value=1, max_value=_MAX_STAGE_COUNT),
    )
    def create_observations(self, stage_count: int, planned_count: int) -> None:
        """Build a generated-width pipeline with a planned prefix of stages."""
        self.stage_count = stage_count
        actual_planned_count = min(planned_count, stage_count)
        for stage_index in range(stage_count):
            command = _echo_cmd()
            events: list[ExecEvent] = []

            def collect(event: ExecEvent, target: list[ExecEvent] = events) -> None:
                """Retain events emitted by this stage's observation."""
                target.append(event)

            observation = _StageObservation(
                cmd=command,
                hooks=_ExecutionHooks(
                    before_hooks=(),
                    after_hooks=(),
                    observe_hooks=(collect,),
                ),
                tags={
                    "pipeline_stage_index": stage_index,
                    "pipeline_stages": stage_count,
                },
                cwd=None,
                env_overlay=None,
                pending_tasks=self.pending_tasks,
                wall_clock=time.time,
            )
            is_planned = stage_index < actual_planned_count
            self.commands.append(command)
            self.observations.append(observation)
            self.events_by_stage.append(events)
            self.planned.append(is_planned)
            self.started.append(False)
            self.exited.append(False)
            self.started_pids.append(None)
            self.exit_codes.append(None)
            self.expected_settlements.append(None)
            if is_planned:
                observation.emit("plan", _EventDetails(pid=None))

    @rule(stage=st.integers(min_value=0, max_value=_MAX_STAGE_COUNT - 1))
    def emit_plan(self, stage: int) -> None:
        """Plan another stage, including repeated plan attempts."""
        if self.completed or stage >= self.stage_count:
            return
        if self.planned[stage] or self.expected_settlements[stage] is not None:
            return
        self.planned[stage] = True
        self.observations[stage].emit("plan", _EventDetails(pid=None))

    @rule(
        stage=st.integers(min_value=0, max_value=_MAX_STAGE_COUNT - 1),
        pid=st.none() | st.integers(min_value=1, max_value=10_000),
    )
    def emit_start(self, stage: int, pid: int | None) -> None:
        """Record a child start with either known or unavailable PID."""
        if self.completed or stage >= self.stage_count:
            return
        if self.expected_settlements[stage] is not None:
            return
        if not self.planned[stage] or self.started[stage]:
            return
        self.started[stage] = True
        self.started_pids[stage] = pid
        self.observations[stage].emit("start", _EventDetails(pid=pid))

    @rule(
        stage=st.integers(min_value=0, max_value=_MAX_STAGE_COUNT - 1),
        exit_code=st.none() | st.integers(min_value=-2, max_value=2),
    )
    def emit_exit(self, stage: int, exit_code: int | None) -> None:
        """Record child status when the model supplies one."""
        if self.completed or stage >= self.stage_count:
            return
        if self.expected_settlements[stage] is not None:
            return
        if not self.started[stage] or self.exited[stage]:
            return
        self.exited[stage] = True
        self.exit_codes[stage] = exit_code
        self.observations[stage].emit(
            "exit",
            _EventDetails(pid=self.started_pids[stage], exit_code=exit_code),
        )

    @rule(stage=st.integers(min_value=0, max_value=_MAX_STAGE_COUNT - 1))
    def timeout_stage(self, stage: int) -> None:
        """Report a timeout and settle a planned stage with known details."""
        if self.completed or stage >= self.stage_count:
            return
        if self.expected_settlements[stage] is not None:
            return
        if not self.planned[stage] or not self.started[stage]:
            return
        self.observations[stage].emit(
            "timeout",
            _EventDetails(
                pid=self.started_pids[stage],
                operation="wait",
                error_type="TimeoutError",
            ),
        )
        self._settle_stage(stage, TerminalOutcome.TIMEOUT)

    @rule(stage=st.integers(min_value=0, max_value=_MAX_STAGE_COUNT - 1))
    def spawn_failure(self, stage: int) -> None:
        """Settle a planned stage that never acquired a child PID."""
        if self.completed or stage >= self.stage_count:
            return
        if not self.planned[stage] or self.started[stage]:
            return
        self._settle_stage(stage, TerminalOutcome.ERROR)

    @rule(stage=st.integers(min_value=0, max_value=_MAX_STAGE_COUNT - 1))
    def cancellation(self, stage: int) -> None:
        """Settle one planned stage after caller cancellation."""
        if self.completed or stage >= self.stage_count:
            return
        self._settle_stage(stage, TerminalOutcome.CANCELLED)

    @rule(
        stage=st.integers(min_value=0, max_value=_MAX_STAGE_COUNT - 1),
        repetitions=st.integers(min_value=2, max_value=4),
    )
    def repeat_cancellation(self, stage: int, repetitions: int) -> None:
        """Attempt the same cancellation settlement repeatedly."""
        if self.completed or stage >= self.stage_count:
            return
        if not self.planned[stage]:
            return
        for _ in range(repetitions):
            self._settle_stage(stage, TerminalOutcome.CANCELLED)

    @rule(stage=st.integers(min_value=0, max_value=_MAX_STAGE_COUNT - 1))
    def settle_completed_stage(self, stage: int) -> None:
        """Classify a reaped stage from its real exit code."""
        if self.completed or stage >= self.stage_count:
            return
        if not self.exited[stage]:
            return
        outcome = (
            TerminalOutcome.EXIT_ZERO
            if self.exit_codes[stage] == 0
            else TerminalOutcome.EXIT_NONZERO
        )
        self._settle_stage(stage, outcome)

    @rule(outcome=st.sampled_from([TerminalOutcome.ERROR, TerminalOutcome.CANCELLED]))
    def attempt_competing_settlement(self, outcome: TerminalOutcome) -> None:
        """Attempt competing error or cancellation outcomes across stages."""
        if self.completed:
            return
        for stage in range(self.stage_count):
            self._settle_stage(stage, outcome)

    @rule(
        exit_codes=st.lists(
            st.integers(min_value=-2, max_value=2),
            min_size=_MAX_STAGE_COUNT,
            max_size=_MAX_STAGE_COUNT,
        )
    )
    def complete_successful_execution(self, exit_codes: list[int]) -> None:
        """Drive generated results through pipeline orchestration finalization."""
        if self.completed or any(self.expected_settlements):
            return
        if any(
            self.planned[index]
            and self.exited[index]
            and self.exit_codes[index] is None
            for index in range(self.stage_count)
        ):
            return

        completion_exit_codes = list(exit_codes)
        for stage_index in range(self.stage_count):
            if not self.planned[stage_index]:
                continue
            observation = self.observations[stage_index]
            if not self.started[stage_index]:
                pid = 10_001 + stage_index
                self.started[stage_index] = True
                self.started_pids[stage_index] = pid
                observation.emit("start", _EventDetails(pid=pid))
            exit_code = self.exit_codes[stage_index]
            if not self.exited[stage_index]:
                exit_code = exit_codes[stage_index]
                self.exited[stage_index] = True
                self.exit_codes[stage_index] = exit_code
                observation.emit(
                    "exit",
                    _EventDetails(
                        pid=self.started_pids[stage_index],
                        exit_code=exit_code,
                    ),
                )
            assert exit_code is not None, (
                "completed stage results must carry a child exit code"
            )
            completion_exit_codes[stage_index] = exit_code
            outcome = (
                TerminalOutcome.EXIT_ZERO
                if exit_code == 0
                else TerminalOutcome.EXIT_NONZERO
            )
            self.expected_settlements[stage_index] = (
                outcome,
                self.started_pids[stage_index],
                exit_code,
            )

        stage_results = [
            _make_result(
                command,
                pid=10_001 + index,
                exit_code=completion_exit_codes[index],
            )
            for index, command in enumerate(self.commands)
        ]
        asyncio.run(
            _finalize_pipeline_execution(
                tuple(self.commands),
                _PipelineObservers(tuple(self.observations), self.pending_tasks),
                stage_results,
                _SinkBracket(None),
            )
        )
        self.completed = True

    def _settle_stage(self, stage: int, outcome: TerminalOutcome) -> None:
        """Record the first modelled outcome and attempt one stage settlement."""
        if self.planned[stage] and self.expected_settlements[stage] is None:
            self.expected_settlements[stage] = (
                outcome,
                self.started_pids[stage],
                self.exit_codes[stage],
            )
        self.observations[stage].emit_terminal(
            outcome,
            _EventDetails(
                pid=self.started_pids[stage],
                exit_code=self.exit_codes[stage],
            ),
        )

    @invariant()
    def planned_stages_settle_once_and_unplanned_stages_stay_unsettled(self) -> None:
        """Check one-shot state after each transition and full completion."""
        for stage in range(self.stage_count):
            self._assert_stage_event_counts(stage)
            self._assert_stage_settlement(stage)
            self._assert_stage_correlation(stage)
            if self.completed and self.planned[stage]:
                assert self.expected_settlements[stage] is not None, (
                    "completed execution must model every planned settlement"
                )

    def _assert_stage_event_counts(self, stage: int) -> None:
        """Check that modelled plan, start, and exit calls emit once."""
        phases = [event.phase for event in self.events_by_stage[stage]]
        assert phases.count("plan") == int(self.planned[stage]), (
            "a planned stage must emit exactly one plan event"
        )
        assert phases.count("start") == int(self.started[stage]), (
            "a stage may start only once after planning"
        )
        assert phases.count("exit") == int(self.exited[stage]), (
            "a stage may report child exit only once after start"
        )

    def _assert_stage_settlement(self, stage: int) -> None:
        """Check settlement presence, order, and first-outcome details."""
        events = self.events_by_stage[stage]
        settlements = [event for event in events if event.phase == "settled"]
        expected = self.expected_settlements[stage]
        if expected is None:
            assert not settlements, "an unplanned or unsettled stage cannot settle"
            return
        assert len(settlements) == 1, (
            "each modelled terminal outcome must emit exactly one settlement"
        )
        assert events[-1].phase == "settled", (
            "settlement must be the final lifecycle event"
        )
        self._assert_settlement_details(settlements[0], expected)

    @staticmethod
    def _assert_settlement_details(
        settlement: ExecEvent,
        expected: tuple[TerminalOutcome, int | None, int | None],
    ) -> None:
        """Check that the first outcome and available child details are kept."""
        assert settlement.terminal_outcome is expected[0], (
            "the first terminal outcome must remain authoritative"
        )
        assert settlement.pid == expected[1], (
            "settlement must retain only the modelled child PID"
        )
        assert settlement.exit_code == expected[2], (
            "settlement must retain only the modelled child status"
        )

    def _assert_stage_correlation(self, stage: int) -> None:
        """Check execution identity and generated pipeline coordinates."""
        events = self.events_by_stage[stage]
        observation = self.observations[stage]
        assert all(event.exec_id == observation.exec_id for event in events), (
            "every stage event must share its execution identity"
        )
        assert all(
            event.tags["pipeline_stage_index"] == stage
            and event.tags["pipeline_stages"] == self.stage_count
            for event in events
        ), "every event must retain its generated pipeline coordinates"


def _make_result(command: SafeCmd, *, pid: int, exit_code: int) -> sh.CommandResult:
    """Build a controlled result for the pipeline's real finalizer."""
    return sh.CommandResult(
        program=command.program,
        argv=command.argv,
        exit_code=exit_code,
        pid=pid,
        stdout=None,
        stderr=None,
        started_at=0.0,
        duration=0.0,
    )


TestSettlementStateMachine = _SettlementStateMachine.TestCase
TestSettlementStateMachine.settings = settings(
    max_examples=35,
    stateful_step_count=30,
    deadline=None,
)
