"""Support generated pipeline settlement tests with controlled child results."""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum import sh
from cuprum._pipeline_internals import _finalize_pipeline_execution
from cuprum._pipeline_types import (
    _EventDetails,
    _PipelineObservers,
    _StageObservation,
)
from cuprum._sink_lifecycle import _SinkBracket
from cuprum.events import TerminalOutcome

if typ.TYPE_CHECKING:
    from cuprum._result_types import _AnyCommandResult
    from cuprum.sh import SafeCmd


class _SettlementMachine(typ.Protocol):
    """State required to drive successful orchestration finalization."""

    stage_count: int
    commands: list[SafeCmd]
    observations: list[_StageObservation]
    planned: list[bool]
    started: list[bool]
    exited: list[bool]
    started_pids: list[int | None]
    exit_codes: list[int | None]
    expected_settlements: list[tuple[TerminalOutcome, int | None, int | None] | None]
    pending_tasks: list[asyncio.Task[None]]
    completed: bool


def _complete_successful_execution(
    machine: _SettlementMachine,
    generated_exit_codes: list[int],
) -> None:
    """Drive eligible generated results through the pipeline finalizer."""
    if not _can_complete_successfully(machine):
        return
    stage_results = _build_successful_stage_results(machine, generated_exit_codes)
    asyncio.run(
        _finalize_pipeline_execution(
            tuple(machine.commands),
            _PipelineObservers(tuple(machine.observations), machine.pending_tasks),
            stage_results,
            _SinkBracket(None),
        )
    )
    machine.completed = True


def _can_complete_successfully(machine: _SettlementMachine) -> bool:
    """Reject completion after settlement or an exit without a known status."""
    if machine.completed:
        return False
    if any(machine.expected_settlements):
        return False
    for stage_index in range(machine.stage_count):
        if _has_planned_exit_without_status(machine, stage_index):
            return False
    return True


def _has_planned_exit_without_status(
    machine: _SettlementMachine,
    stage_index: int,
) -> bool:
    """Identify a planned child exit that cannot produce a result outcome."""
    if not machine.planned[stage_index]:
        return False
    if not machine.exited[stage_index]:
        return False
    return machine.exit_codes[stage_index] is None


def _build_successful_stage_results(
    machine: _SettlementMachine,
    generated_exit_codes: list[int],
) -> list[_AnyCommandResult]:
    """Prepare planned stage events and assemble finalizer result values."""
    stage_results: list[_AnyCommandResult] = []
    for stage_index, command in enumerate(machine.commands):
        exit_code = machine.exit_codes[stage_index]
        if exit_code is None:
            exit_code = generated_exit_codes[stage_index]
        if machine.planned[stage_index]:
            _prepare_successful_stage(machine, stage_index, exit_code)
        stage_results.append(
            _make_result(
                command,
                pid=10_001 + stage_index,
                exit_code=exit_code,
            )
        )
    return stage_results


def _prepare_successful_stage(
    machine: _SettlementMachine,
    stage_index: int,
    exit_code: int,
) -> None:
    """Fill missing start and exit facts before finalizing a planned stage."""
    observation = machine.observations[stage_index]
    if not machine.started[stage_index]:
        pid = 10_001 + stage_index
        machine.started[stage_index] = True
        machine.started_pids[stage_index] = pid
        observation.emit("start", _EventDetails(pid=pid))
    if not machine.exited[stage_index]:
        machine.exited[stage_index] = True
        machine.exit_codes[stage_index] = exit_code
        observation.emit(
            "exit",
            _EventDetails(
                pid=machine.started_pids[stage_index],
                exit_code=exit_code,
            ),
        )
    machine.expected_settlements[stage_index] = (
        _outcome_for_exit_code(exit_code),
        machine.started_pids[stage_index],
        exit_code,
    )


def _outcome_for_exit_code(exit_code: int) -> TerminalOutcome:
    """Classify a child status for the generated successful execution."""
    if exit_code == 0:
        return TerminalOutcome.EXIT_ZERO
    return TerminalOutcome.EXIT_NONZERO


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
