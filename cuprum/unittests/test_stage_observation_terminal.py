"""Terminal-event state transitions on pipeline stage observations."""

from __future__ import annotations

from cuprum._pipeline_types import _EventDetails, _ExecutionHooks, _StageObservation
from cuprum.events import ExecEvent, TerminalOutcome
from cuprum.unittests._cqrs_fixtures import _echo_cmd


def test_stage_observation_emits_only_the_first_terminal_outcome() -> None:
    """Terminal observation is take-once and reuses the execution identity."""
    events: list[ExecEvent] = []

    def collect(event: ExecEvent) -> None:
        """Retain events for lifecycle assertions."""
        events.append(event)

    observation = _StageObservation(
        cmd=_echo_cmd(),
        hooks=_ExecutionHooks(
            before_hooks=(),
            after_hooks=(),
            observe_hooks=(collect,),
        ),
        tags={},
        cwd=None,
        env_overlay=None,
        pending_tasks=[],
        wall_clock=lambda: 1.0,
    )
    observation.emit("plan", _EventDetails(pid=None))
    observation.emit_terminal(
        TerminalOutcome.ERROR,
        _EventDetails(pid=None, exit_code=None),
    )
    observation.emit_terminal(
        TerminalOutcome.CANCELLED,
        _EventDetails(pid=None, exit_code=None),
    )

    terminal_events = [event for event in events if event.phase == "settled"]
    assert len(terminal_events) == 1, "only the first terminal call should emit"
    assert terminal_events[0].terminal_outcome is TerminalOutcome.ERROR, (
        "the first terminal outcome should win"
    )
    assert terminal_events[0].exec_id == events[0].exec_id, (
        "terminal events should reuse the observation's execution identity"
    )
    assert terminal_events[0].exit_code is None, (
        "an execution without a child status must not invent one"
    )
