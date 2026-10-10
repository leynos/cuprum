"""Behavioural tests for terminal execution outcomes."""

from __future__ import annotations

import sys
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from cuprum import sh
from cuprum.catalogue import ProgramCatalogue
from cuprum.events import ExecEvent, TerminalOutcome
from cuprum.program import Program
from tests.behaviour._execution_runtime_support import (
    WorkerCommand,
    _cancel_command_with_grace,
    _CancellationOptions,
    _create_worker_command,
)
from tests.helpers.timeouts import wait_for_process_death

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.sh import SafeCmd

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Cancellation cleanup process checks rely on POSIX signal semantics.",
)


class _TerminalOutcomeState(typ.TypedDict, total=False):
    """Values shared by terminal-outcome behaviour steps."""

    events: list[ExecEvent]
    pid: int


@pytest.fixture
def terminal_outcome_state() -> _TerminalOutcomeState:
    """Create mutable state local to one terminal-outcome scenario."""
    return {}


@scenario(
    "../features/execution_runtime.feature",
    "Cancellation settles while observe-hook cleanup drains",
)
def test_cancellation_settles_during_hook_cleanup() -> None:
    """Cancellation emits a terminal outcome while hooks drain."""


@scenario(
    "../features/execution_runtime.feature",
    "Repeated cancellation settles while observe-hook cleanup drains",
)
def test_repeated_cancellation_settles_during_hook_cleanup() -> None:
    """Repeated cancellation still emits one terminal outcome."""


@scenario(
    "../features/execution_runtime.feature",
    "Spawn failure has a terminal execution outcome",
)
def test_spawn_failure_has_terminal_outcome() -> None:
    """A registered executable that cannot spawn still settles observation."""


@given(
    "a long running command for terminal outcome observation",
    target_fixture="terminal_worker_command",
)
def given_terminal_worker_command(tmp_path: Path) -> WorkerCommand:
    """Construct a cooperative worker for cancellation scenarios."""
    return _create_worker_command(
        tmp_path,
        script_name="terminal_outcome_worker.py",
        cooperative=True,
    )


def _cancel_worker_during_terminal_cleanup(
    state: _TerminalOutcomeState,
    worker: WorkerCommand,
    *,
    repeat_cancellations: int = 0,
) -> None:
    """Run the worker cancellation scenario and retain its lifecycle events."""
    events: list[ExecEvent] = []
    state["pid"] = _cancel_command_with_grace(
        worker["command"],
        worker["pid_file"],
        options=_CancellationOptions(
            events=events,
            repeat_cancellations=repeat_cancellations,
        ),
    )
    state["events"] = events


@when("I cancel while terminal cleanup is observed")
def when_cancel_during_terminal_cleanup(
    terminal_outcome_state: _TerminalOutcomeState,
    terminal_worker_command: WorkerCommand,
) -> None:
    """Cancel after start while an async terminal hook holds cleanup open."""
    _cancel_worker_during_terminal_cleanup(
        terminal_outcome_state, terminal_worker_command
    )


@when("I cancel repeatedly during terminal cleanup")
def when_repeat_cancellation_during_terminal_cleanup(
    terminal_outcome_state: _TerminalOutcomeState,
    terminal_worker_command: WorkerCommand,
) -> None:
    """Repeat cancellation while terminal-hook cleanup remains pending."""
    _cancel_worker_during_terminal_cleanup(
        terminal_outcome_state,
        terminal_worker_command,
        repeat_cancellations=3,
    )


def _assert_single_terminal_outcome(
    events: list[ExecEvent],
    expected_outcome: TerminalOutcome,
) -> None:
    """Assert one correlated terminal outcome with no invented process data."""
    settled = [event for event in events if event.phase == "settled"]
    plans = [event for event in events if event.phase == "plan"]
    assert len(settled) == 1, f"Expected one settled event, found {settled!r}"
    assert len(plans) == 1, f"Expected one plan event, found {plans!r}"
    assert settled[0].terminal_outcome is expected_outcome, (
        f"expected {expected_outcome!s} as the terminal category"
    )
    assert settled[0].exec_id == plans[0].exec_id, (
        "settlement must correlate with its plan event"
    )
    assert settled[0].pid is None, "a run without a spawned child has no PID"
    assert settled[0].exit_code is None, "a run without a child has no exit code"


@then("exactly one cancelled terminal outcome is observed")
def then_one_cancelled_terminal_outcome(
    terminal_outcome_state: _TerminalOutcomeState,
) -> None:
    """Assert settlement is unique and correlated with the observed run."""
    _assert_single_terminal_outcome(
        terminal_outcome_state["events"],
        TerminalOutcome.CANCELLED,
    )


@then("the terminal-outcome subprocess stops cleanly")
def then_terminal_subprocess_stops_cleanly(
    terminal_outcome_state: _TerminalOutcomeState,
) -> None:
    """Assert cancellation cleanup terminates the child process."""
    wait_for_process_death(
        terminal_outcome_state["pid"],
        seconds=5.0,
        context="terminal execution outcome cancellation",
    )


@given(
    "a registered but absent terminal-outcome executable",
    target_fixture="absent_terminal_command",
)
def given_registered_absent_terminal_executable(tmp_path: Path) -> SafeCmd:
    """Build an allowlisted command whose absolute path does not exist."""
    absent_program = Program(str(tmp_path / "absent-executable"))
    catalogue = ProgramCatalogue.from_programs(absent_program)
    return sh.make(absent_program, catalogue=catalogue)()


@when("I run it with an observe hook")
def when_run_absent_terminal_executable(
    terminal_outcome_state: _TerminalOutcomeState,
    absent_terminal_command: SafeCmd,
) -> None:
    """Run the absent executable and retain its events after spawn failure."""
    events: list[ExecEvent] = []
    with sh.observe(events.append), pytest.raises(FileNotFoundError):
        absent_terminal_command.run_sync()
    terminal_outcome_state["events"] = events


@then("spawn failure emits one error terminal outcome")
def then_spawn_failure_settles(
    terminal_outcome_state: _TerminalOutcomeState,
) -> None:
    """Assert failed spawn has one correlated event and no fabricated status."""
    _assert_single_terminal_outcome(
        terminal_outcome_state["events"],
        TerminalOutcome.ERROR,
    )
