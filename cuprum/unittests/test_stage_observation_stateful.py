"""Model the exactly-once terminal lifecycle across pipeline stages."""

from __future__ import annotations

import time

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from cuprum._pipeline_types import _EventDetails, _ExecutionHooks, _StageObservation
from cuprum.events import ExecEvent, TerminalOutcome
from cuprum.unittests._cqrs_fixtures import _echo_cmd

_STAGE_COUNT = 3


class _SettlementStateMachine(RuleBasedStateMachine):
    """Generate lifecycle event orderings for a small pipeline model."""

    def __init__(self) -> None:
        """Create independent stage observations and their expected state."""
        super().__init__()
        self.observations: list[_StageObservation] = []
        self.events_by_stage: list[list[ExecEvent]] = []
        self.planned = [False] * _STAGE_COUNT
        self.started = [False] * _STAGE_COUNT
        self.exited = [False] * _STAGE_COUNT
        self.started_pids: list[int | None] = [None] * _STAGE_COUNT
        self.exit_codes: list[int | None] = [None] * _STAGE_COUNT
        self.expected_settlements: list[
            tuple[TerminalOutcome, int | None, int | None] | None
        ] = [None] * _STAGE_COUNT
        for stage_index in range(_STAGE_COUNT):
            events: list[ExecEvent] = []

            def collect(event: ExecEvent, target: list[ExecEvent] = events) -> None:
                """Retain events for this modelled pipeline stage."""
                target.append(event)

            self.events_by_stage.append(events)
            self.observations.append(
                _StageObservation(
                    cmd=_echo_cmd(),
                    hooks=_ExecutionHooks(
                        before_hooks=(),
                        after_hooks=(),
                        observe_hooks=(collect,),
                    ),
                    tags={
                        "pipeline_stage_index": stage_index,
                        "pipeline_stages": _STAGE_COUNT,
                    },
                    cwd=None,
                    env_overlay=None,
                    pending_tasks=[],
                    wall_clock=time.time,
                )
            )

    @rule(stage=st.integers(min_value=0, max_value=_STAGE_COUNT - 1))
    def emit_plan(self, stage: int) -> None:
        """Plan a stage, including repeated plan attempts."""
        if self.planned[stage] or self.expected_settlements[stage] is not None:
            return
        self.planned[stage] = True
        self.observations[stage].emit("plan", _EventDetails(pid=None))

    @rule(
        stage=st.integers(min_value=0, max_value=_STAGE_COUNT - 1),
        pid=st.none() | st.integers(min_value=1, max_value=10_000),
    )
    def emit_start(self, stage: int, pid: int | None) -> None:
        """Record a child start with either known or unavailable PID."""
        if self.expected_settlements[stage] is not None:
            return
        if not self.planned[stage] or self.started[stage]:
            return
        self.started[stage] = True
        self.started_pids[stage] = pid
        self.observations[stage].emit("start", _EventDetails(pid=pid))

    @rule(
        stage=st.integers(min_value=0, max_value=_STAGE_COUNT - 1),
        exit_code=st.none() | st.integers(min_value=-2, max_value=2),
    )
    def emit_exit(self, stage: int, exit_code: int | None) -> None:
        """Record child status when the model supplies one."""
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

    @rule(stage=st.integers(min_value=0, max_value=_STAGE_COUNT - 1))
    def timeout_stage(self, stage: int) -> None:
        """Report a timeout and settle a planned stage with known details."""
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

    @rule(stage=st.integers(min_value=0, max_value=_STAGE_COUNT - 1))
    def spawn_failure(self, stage: int) -> None:
        """Settle a planned stage that never acquired a child PID."""
        if not self.planned[stage] or self.started[stage]:
            return
        self._settle_stage(stage, TerminalOutcome.ERROR)

    @rule(stage=st.integers(min_value=0, max_value=_STAGE_COUNT - 1))
    def cancellation(self, stage: int) -> None:
        """Settle one planned stage after caller cancellation."""
        self._settle_stage(stage, TerminalOutcome.CANCELLED)

    @rule(stage=st.integers(min_value=0, max_value=_STAGE_COUNT - 1))
    def settle_completed_stage(self, stage: int) -> None:
        """Classify a reaped stage from its real exit code."""
        if not self.exited[stage]:
            return
        outcome = (
            TerminalOutcome.EXIT_ZERO
            if self.exit_codes[stage] == 0
            else TerminalOutcome.EXIT_NONZERO
        )
        self._settle_stage(stage, outcome)

    @rule(outcome=st.sampled_from([TerminalOutcome.ERROR, TerminalOutcome.CANCELLED]))
    def attempt_settlement(self, outcome: TerminalOutcome) -> None:
        """Attempt a competing error or cancellation, including repeats."""
        for stage in range(_STAGE_COUNT):
            self._settle_stage(stage, outcome)

    def _settle_stage(self, stage: int, outcome: TerminalOutcome) -> None:
        """Record the model's first outcome and attempt one stage settlement."""
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
    def each_stage_has_at_most_one_correlated_settlement(self) -> None:
        """Every stage obeys plan gating, first-outcome, and detail contracts."""
        for stage in range(_STAGE_COUNT):
            self._assert_stage_event_counts(stage)
            self._assert_stage_settlement(stage)
            self._assert_stage_correlation(stage)

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
            assert not settlements, "an unplanned stage cannot settle"
            return
        assert len(settlements) == 1, "a planned stage must settle at most once"
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
        """Check execution identity and pipeline coordinates on every event."""
        events = self.events_by_stage[stage]
        observation = self.observations[stage]
        assert all(event.exec_id == observation.exec_id for event in events), (
            "every stage event must share its execution identity"
        )
        assert all(
            event.tags["pipeline_stage_index"] == stage
            and event.tags["pipeline_stages"] == _STAGE_COUNT
            for event in events
        ), "every event must retain its pipeline stage coordinates"


TestSettlementStateMachine = _SettlementStateMachine.TestCase
TestSettlementStateMachine.settings = settings(
    max_examples=35,
    stateful_step_count=30,
    deadline=None,
)
