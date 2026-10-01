"""Per-stage terminal events and result assembly for pipelines.

Split from ``cuprum._pipeline_internals`` so that module stays about *running*
a pipeline — spawning, waiting, cleanup — while the rules for reporting each
stage live here: the actual child ``exit`` event, definitive ``settled`` event,
and ``CommandResult`` assembled alongside them.

Both the success path and the timeout path emit that terminal event from this
module, so a stage never reports a ``timeout`` and then falls silent.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import typing as typ

from cuprum._pipeline_collect import _sh_module
from cuprum._pipeline_types import _EventDetails
from cuprum._sink_lifecycle import _outcome_for_result
from cuprum._timeout_reporting import _safe_emit_terminal
from cuprum.events import ResourceUsageMode, TerminalOutcome

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import (
        _PipelineSpawnResult,
        _PipelineStageResultInputs,
        _StageObservation,
    )
    from cuprum.sh import CommandResult, SafeCmd

# Every pipeline stage's terminal event reports this. Both exits below are
# terminal events that attempted no measurement, and a stage can never attempt
# one: its children are reaped concurrently, so no per-child interface can
# attribute usage to it. Saying so explicitly is what keeps the contract on
# ``ExecEvent.resource_usage_mode`` — set on every terminal event, ``None`` on
# every other phase — true for stages as well as for direct commands, whose
# returned ``CommandResult`` likewise leaves all three figures ``None``.
_STAGE_RESOURCE_MODE: typ.Final = ResourceUsageMode.UNAVAILABLE


def _emit_terminal_events(
    observations: tuple[_StageObservation, ...],
    outcome: TerminalOutcome,
    *,
    processes: cabc.Sequence[asyncio.subprocess.Process] = (),
    started_at: cabc.Sequence[float] = (),
) -> None:
    """Settle every planned stage with the facts known after cleanup."""
    ended_at = time.perf_counter()
    for idx, observation in enumerate(observations):
        process = processes[idx] if idx < len(processes) else None
        started = started_at[idx] if idx < len(started_at) else None
        _safe_emit_terminal(
            observation,
            outcome,
            _EventDetails(
                pid=None if process is None else process.pid,
                exit_code=None if process is None else process.returncode,
                duration_s=(None if started is None else max(0.0, ended_at - started)),
            ),
        )


def _emit_result_terminal_events(
    observations: tuple[_StageObservation, ...],
    stage_results: list[CommandResult],
    *,
    outcome: TerminalOutcome | None = None,
    best_effort: bool = False,
) -> None:
    """Settle every assembled stage, surfacing the first hook failure by default."""
    first_error: BaseException | None = None
    for observation, result in zip(observations, stage_results, strict=True):
        stage_outcome = (
            _outcome_for_result(result).outcome if outcome is None else outcome
        )
        try:
            observation.emit_terminal(
                stage_outcome,
                _EventDetails(
                    pid=observation.started_pid,
                    exit_code=result.exit_code,
                    duration_s=result.duration,
                ),
            )
        # Try all stage observers even for fatal hook failures, then preserve
        # the first one so cleanup of the other stages cannot mask it.
        except BaseException as exc:  # ruff: ignore[blind-except]
            if first_error is None:
                first_error = exc
    if first_error is not None and not best_effort:
        raise first_error


def _emit_timeout_exit_events(
    observations: tuple[_StageObservation, ...],
    spawn: _PipelineSpawnResult,
) -> None:
    """Emit the terminal ``exit`` event for every stage reaped by a timeout.

    The success path emits these from :func:`_build_pipeline_stage_results`,
    which a timeout never reaches — it raises out of `_collect_pipeline_inputs`
    first. The single-command path has no such gap, since
    `_handle_subprocess_timeout` emits ``exit`` before raising
    ``TimeoutExpired``, so without this a pipeline stage would be the only
    execution that reports a ``timeout`` and then goes quiet.

    That matters beyond symmetry: ``TracingHook`` keeps a span open through
    ``exit`` and closes it only on ``settled``, so a missing terminal event
    leaves the stage's span open for the lifetime of the tracer.

    Every stage has been terminated and reaped by this point, so ``returncode``
    is available; ``-1`` stands in for a stage with no recorded code, matching
    ``_get_exit_code``.

    Each stage emits best-effort, matching ``_timeout_reporting._safe_emit``.
    :meth:`_StageObservation.emit` re-raises a synchronous observe-hook
    failure, and this runs inside the caller's ``except TimeoutExpired``
    handler: letting one escape would replace the ``TimeoutExpired`` the caller
    is about to re-raise *and* abandon the remaining stages, leaving exactly
    the open spans this function exists to close. ``emit`` records any
    scheduled async-hook tasks before raising, so those are still drained by
    the runner; only the synchronous failure is swallowed.
    """
    ended_at = time.perf_counter()
    for idx, obs in enumerate(observations):
        process = spawn.processes[idx]
        with contextlib.suppress(Exception, asyncio.CancelledError):
            obs.emit(
                "exit",
                _EventDetails(
                    pid=process.pid,
                    exit_code=(
                        process.returncode if process.returncode is not None else -1
                    ),
                    duration_s=max(0.0, ended_at - spawn.stages.started_at[idx]),
                    resource_usage_mode=_STAGE_RESOURCE_MODE,
                ),
            )


def _build_pipeline_stage_results(
    parts: tuple[SafeCmd, ...],
    observations: tuple[_StageObservation, ...],
    *,
    processes: list[asyncio.subprocess.Process],
    inputs: _PipelineStageResultInputs,
) -> list[CommandResult]:
    """Emit exit events and assemble a command result per pipeline stage."""
    sh = _sh_module()
    stage_results: list[CommandResult] = []
    for idx, obs in enumerate(observations):
        process = processes[idx]
        ended_at = inputs.wait_result.ended_at[idx]
        duration_s = (
            None
            if ended_at is None
            else max(0.0, ended_at - inputs.wait_result.started_at[idx])
        )
        obs.emit(
            "exit",
            _EventDetails(
                pid=process.pid,
                exit_code=inputs.wait_result.exit_codes[idx],
                duration_s=duration_s,
                resource_usage_mode=_STAGE_RESOURCE_MODE,
            ),
        )
        stage_result = sh.CommandResult(
            program=obs.cmd.program,
            argv=obs.cmd.argv,
            exit_code=inputs.wait_result.exit_codes[idx],
            pid=process.pid if process.pid is not None else -1,
            stdout=inputs.final_stdout if idx == len(parts) - 1 else None,
            stderr=inputs.stderr_by_stage[idx],
            started_at=inputs.wait_result.wall_clock_started_at[idx],
            duration=0.0 if duration_s is None else duration_s,
            max_rss_bytes=None,
            user_cpu_seconds=None,
            system_cpu_seconds=None,
            relay_fallbacks=inputs.relay_fallbacks_by_stage[idx],
            resolved_path=obs.resolved_path,
        )
        stage_results.append(stage_result)
    return stage_results


__all__ = [
    "_build_pipeline_stage_results",
    "_emit_result_terminal_events",
    "_emit_terminal_events",
    "_emit_timeout_exit_events",
]
