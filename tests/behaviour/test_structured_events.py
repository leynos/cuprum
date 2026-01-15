"""Behavioural tests for structured execution events and telemetry hooks."""

from __future__ import annotations

import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from cuprum import sh
from cuprum.context import ScopeConfig, scoped
from cuprum.sh import ExecutionContext
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent


@scenario(
    "../features/structured_events.feature",
    "Observe hook receives output events and timing metadata",
)
def test_observe_hook_receives_output_and_timing() -> None:
    """Behavioural coverage for structured output and timing events."""


@pytest.fixture
def behaviour_state() -> dict[str, object]:
    """Shared mutable state for behaviour scenarios."""
    return {}


@given(
    "a safe command that writes to stdout and stderr",
    target_fixture="observed_command",
)
def given_observed_command() -> dict[str, object]:
    """Build a SafeCmd that writes to stdout and stderr."""
    catalogue, python_program = python_catalogue()
    script = "\n".join(
        (
            "import sys",
            "print('out1')",
            "print('out2')",
            "print('err1', file=sys.stderr)",
        ),
    )
    cmd = sh.make(python_program, catalogue=catalogue)("-c", script)
    return {"catalogue": catalogue, "cmd": cmd}


@when("I run the command with an observe hook")
def when_run_with_observe_hook(
    behaviour_state: dict[str, object],
    observed_command: dict[str, object],
) -> None:
    """Run the command while collecting observe events."""
    catalogue = typ.cast("typ.Any", observed_command["catalogue"])
    cmd = typ.cast("typ.Any", observed_command["cmd"])

    events: list[ExecEvent] = []

    def hook(ev: ExecEvent) -> None:
        events.append(ev)

    with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
        _ = cmd.run_sync(context=ExecutionContext(tags={"run_id": "bdd"}))

    behaviour_state["events"] = events


@then("the observe hook sees stdout and stderr line events")
def then_observe_sees_output_events(behaviour_state: dict[str, object]) -> None:
    """Validate observed output line events."""
    events = typ.cast("list[ExecEvent]", behaviour_state["events"])
    stdout_lines = {ev.line for ev in events if ev.phase == "stdout"}
    stderr_lines = {ev.line for ev in events if ev.phase == "stderr"}
    assert "out1" in stdout_lines
    assert "out2" in stdout_lines
    assert "err1" in stderr_lines


@then("the observe hook sees timing and tag metadata")
def then_observe_sees_timing_and_tags(behaviour_state: dict[str, object]) -> None:
    """Validate event timing and tag metadata."""
    events = typ.cast("list[ExecEvent]", behaviour_state["events"])
    start = next(ev for ev in events if ev.phase == "start")
    exit_ = next(ev for ev in events if ev.phase == "exit")

    assert start.pid is not None
    assert start.pid > 0
    assert exit_.exit_code == 0
    assert exit_.duration_s is not None
    assert exit_.duration_s >= 0.0

    for ev in (start, exit_):
        assert ev.tags["run_id"] == "bdd"
        assert "project" in ev.tags
