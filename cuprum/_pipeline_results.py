"""Per-stage terminal events and result assembly for pipelines.

Split from ``cuprum._pipeline_internals`` so that module stays about *running*
a pipeline — spawning, waiting, cleanup — while the rules for *reporting* each
stage live here: the terminal ``exit`` event a stage owes its observers, and
the ``CommandResult`` assembled alongside it.

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

if typ.TYPE_CHECKING:
    from cuprum._pipeline_types import (
        _PipelineSpawnResult,
        _PipelineStageResultInputs,
        _StageObservation,
    )
    from cuprum.sh import CommandResult, SafeCmd


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

    That matters beyond symmetry: ``TracingHook`` ends a span only on ``exit``,
    so a missing terminal event leaves the stage's span open for the lifetime
    of the tracer.

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
            ),
        )
        stage_results.append(
            sh.CommandResult(
                program=obs.cmd.program,
                argv=obs.cmd.argv,
                exit_code=inputs.wait_result.exit_codes[idx],
                pid=process.pid if process.pid is not None else -1,
                stdout=inputs.final_stdout if idx == len(parts) - 1 else None,
                stderr=inputs.stderr_by_stage[idx],
                relay_fallbacks=inputs.relay_fallbacks_by_stage[idx],
            ),
        )
    return stage_results


__all__ = [
    "_build_pipeline_stage_results",
    "_emit_timeout_exit_events",
]
