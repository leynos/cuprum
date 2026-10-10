"""Observe lifecycle outcomes for successful, failed, and rejected runs."""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum.catalogue import ProgramCatalogue, UnknownProgramError
from cuprum.context import ForbiddenProgramError
from cuprum.events import ExecEvent, TerminalOutcome
from cuprum.program import Program
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_pre_observation_validation_emits_no_lifecycle_events() -> None:
    """Catalogue and allowlist rejection happen before observation begins."""
    catalogue, python_program = python_catalogue()
    command = sh.make(python_program, catalogue=catalogue)("-c", "pass")
    events: list[ExecEvent] = []

    with (
        scoped(ScopeConfig(allowlist=frozenset())),
        sh.observe(events.append),
        pytest.raises(ForbiddenProgramError),
    ):
        command.run_sync()

    assert not events, "allowlist rejection must precede plan and settlement"

    with sh.observe(events.append), pytest.raises(UnknownProgramError):
        sh.make(Program("uncataloged"), catalogue=catalogue)

    assert not events, "catalogue rejection must precede plan and settlement"


def test_spawn_failure_settles_without_inventing_a_child_status(
    tmp_path: Path,
) -> None:
    """A planned run that cannot spawn still has one correlated outcome."""
    absent_program = Program(str(tmp_path / "absent-executable"))
    catalogue = ProgramCatalogue.from_programs(absent_program)
    command = sh.make(absent_program, catalogue=catalogue)()
    events: list[ExecEvent] = []

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(events.append),
        pytest.raises(FileNotFoundError),
    ):
        command.run_sync()

    plans = [event for event in events if event.phase == "plan"]
    settled = [event for event in events if event.phase == "settled"]
    assert len(plans) == len(settled) == 1, (
        f"the planned spawn failure needs one terminal event, got "
        f"{[event.phase for event in events]}"
    )
    terminal = settled[0]
    assert terminal.terminal_outcome is TerminalOutcome.ERROR, (
        "spawn failures must settle as errors"
    )
    assert terminal.exec_id == plans[0].exec_id, (
        "settlement must preserve the planned execution identity"
    )
    assert terminal.pid is None, "spawn failure cannot invent a child PID"
    assert terminal.exit_code is None, "spawn failure cannot invent an exit code"


def test_nonzero_child_status_is_preserved_at_settlement() -> None:
    """A non-zero exit settles with the process's actual status and identity."""
    catalogue, python_program = python_catalogue()
    command = sh.make(python_program, catalogue=catalogue)("-c", "raise SystemExit(23)")
    events: list[ExecEvent] = []

    with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(events.append):
        result = command.run_sync()

    exit_event = next(event for event in events if event.phase == "exit")
    terminal = next(event for event in events if event.phase == "settled")
    assert result.exit_code == 23, "the result must retain the child's status"
    assert terminal.terminal_outcome is TerminalOutcome.EXIT_NONZERO, (
        "a non-zero child exit must settle as exit_nonzero"
    )
    assert terminal.exec_id == exit_event.exec_id, (
        "settlement must correlate with the child exit"
    )
    assert terminal.pid == exit_event.pid, "settlement must retain the real PID"
    assert terminal.exit_code == 23, "settlement must retain the real exit code"


def test_success_settles_after_exit_with_child_details() -> None:
    """A successful settlement follows exit and retains its child details."""
    catalogue, python_program = python_catalogue()
    command = sh.make(python_program, catalogue=catalogue)("-c", "pass")
    events: list[ExecEvent] = []

    with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(events.append):
        command.run_sync()

    exit_event = next(event for event in events if event.phase == "exit")
    terminal = next(event for event in events if event.phase == "settled")
    assert [event.phase for event in events[-2:]] == ["exit", "settled"], (
        "successful settlement must follow the child's exit"
    )
    assert terminal.terminal_outcome is TerminalOutcome.EXIT_ZERO, (
        "a zero exit must settle as exit_zero"
    )
    assert terminal.exec_id == exit_event.exec_id, (
        "settlement must correlate with the child exit"
    )
    assert terminal.pid == exit_event.pid, "settlement must retain the real PID"
    assert terminal.exit_code == exit_event.exit_code == 0, (
        "settlement must retain the child's zero exit code"
    )


def test_immediate_timeout_settles_after_preserved_exit() -> None:
    """A timeout remains the final category after the child exit is recorded."""
    catalogue, python_program = python_catalogue()
    command = sh.make(python_program, catalogue=catalogue)(
        "-c", "import time; time.sleep(30)"
    )
    events: list[ExecEvent] = []

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(events.append),
        pytest.raises(sh.TimeoutExpired),
    ):
        command.run_sync(timeout=0)

    exit_event = next(event for event in events if event.phase == "exit")
    terminal = next(event for event in events if event.phase == "settled")
    assert [event.phase for event in events[-2:]] == ["exit", "settled"], (
        "timeout settlement must follow the preserved child exit"
    )
    assert terminal.terminal_outcome is TerminalOutcome.TIMEOUT, (
        "a timed-out child must settle as timeout"
    )
    assert terminal.exec_id == exit_event.exec_id, (
        "timeout settlement must correlate with the child exit"
    )
    assert terminal.pid == exit_event.pid, "timeout settlement must retain the PID"
    assert terminal.exit_code is None, (
        "timeout settlement must not invent a child exit code"
    )
