"""Finalization for a pipeline run, however it ends.

Split from ``cuprum._pipeline_internals``, which keeps the *spawn* half of a
pipeline run. Everything here runs once the stages already exist, so every path
owes them teardown: the after-hooks and observe-task drain of a completed run, a
failed stage-result build, a run failure that still has live stream tasks, and
the deadline path that has already terminated the stages before reporting the
expiry.

The three steps of a completed run are ordered deliberately, and the ordering is
the reason they live behind one helper: an after-hook that raises is a terminal
*run* error, so the outcome a sink adapter records has to be decided after the
hooks have had their say rather than from the stage results the run happened to
produce before the hook ran. Callers reach these helpers through
``cuprum._pipeline_internals``, which re-exports the spawn-side half of the same
run.
"""

from __future__ import annotations

import typing as typ

from cuprum._observability import (
    _drain_tasks_during_cleanup,
    _wait_for_exec_hook_tasks,
)
from cuprum._pipeline_results import (
    _emit_result_terminal_events,
    _emit_terminal_events,
    _emit_timeout_exit_events,
)
from cuprum._pipeline_sink import _pipeline_result_outcome
from cuprum._pipeline_stream_results import _cancel_stream_tasks
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._sink_lifecycle import _outcome_for_error, _SinkBracket
from cuprum._timeout_reporting import _report_pipeline_timeout_expiry

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc

    from cuprum._pipeline_config import _PipelineRunConfig
    from cuprum._pipeline_types import (
        _ExecutionHooks,
        _PipelineObservers,
        _PipelineSpawnResult,
    )
    from cuprum.sh import CommandResult, SafeCmd


_PIPELINE_FINALIZATION_ERROR = "pipeline finalization failed"


async def _finalize_pipeline_execution(
    parts: tuple[SafeCmd, ...],
    observers: _PipelineObservers,
    stage_results: list[CommandResult],
    sink_bracket: _SinkBracket,
) -> None:
    """Run after-hooks before settling stages, then drain observer tasks.

    After-hook failures determine the run outcome. The sink bracket is
    take-once; shielded drains keep pending hooks alive through cancellation.
    """
    observations = observers.observations
    pending_tasks = observers.pending_tasks
    hooks_by_stage = tuple(obs.hooks for obs in observations)
    try:
        _run_pipeline_after_hooks(parts, hooks_by_stage, stage_results)
    except BaseException as after_hook_error:
        outcome = _outcome_for_error(after_hook_error)
        sink_bracket.close(outcome=outcome)
        _emit_result_terminal_events(
            observations,
            stage_results,
            outcome=outcome.outcome,
            best_effort=True,
        )
        await _shielded_cleanup(
            _drain_tasks_during_cleanup(
                pending_tasks, after_hook_error, message=_PIPELINE_FINALIZATION_ERROR
            )
        )
        raise
    sink_bracket.close(outcome=_pipeline_result_outcome(stage_results))
    try:
        _emit_result_terminal_events(observations, stage_results)
    except BaseException as terminal_error:
        await _shielded_cleanup(
            _drain_tasks_during_cleanup(
                pending_tasks,
                terminal_error,
                message=_PIPELINE_FINALIZATION_ERROR,
            )
        )
        raise
    await _shielded_cleanup(_wait_for_exec_hook_tasks(pending_tasks))


async def _reconcile_pipeline_run_failure(
    spawn: _PipelineSpawnResult,
    pending_tasks: list[asyncio.Task[None]],
    run_error: BaseException,
    after_process_cleanup: cabc.Callable[[], None],
) -> None:
    """Cancel the stream tasks and drain the observe tasks after a run failure.

    Kept as one coroutine so the caller can shield both halves together: the
    stream tasks and the observe-hook tasks are all owned by the pipeline, and
    a cancellation arriving between two separately shielded steps would leave
    the second set pending.
    """
    await _cancel_stream_tasks(spawn.stderr_tasks, spawn.stdout_task)
    after_process_cleanup()
    await _drain_tasks_during_cleanup(
        pending_tasks, run_error, message=_PIPELINE_FINALIZATION_ERROR
    )


async def _finalize_pipeline_run_failure(
    config: _PipelineRunConfig,
    spawn: _PipelineSpawnResult,
    observers: _PipelineObservers,
    run_error: BaseException,
) -> None:
    """Close the sink, settle spawned stages, and drain after run failure."""
    outcome = _outcome_for_error(run_error)
    config.sink_bracket.close(outcome=outcome)
    await _shielded_cleanup(
        _reconcile_pipeline_run_failure(
            spawn,
            observers.pending_tasks,
            run_error,
            lambda: _emit_terminal_events(
                observers.observations,
                outcome.outcome,
                processes=spawn.processes,
                started_at=spawn.stages.started_at,
            ),
        )
    )


async def _finalize_pipeline_timeout(
    config: _PipelineRunConfig,
    spawn: _PipelineSpawnResult,
    observers: _PipelineObservers,
    timeout_error: BaseException,
) -> None:
    """Report a pipeline timeout and finalize its sink and observe tasks."""
    observations = observers.observations
    config.sink_bracket.close(outcome=_outcome_for_error(timeout_error))
    _report_pipeline_timeout_expiry(
        observations,
        spawn.processes,
        configured_timeout=config.timeout,
    )
    _emit_timeout_exit_events(observations, spawn)
    _emit_terminal_events(
        observations,
        _outcome_for_error(timeout_error).outcome,
        processes=spawn.processes,
        started_at=spawn.stages.started_at,
    )
    await _shielded_cleanup(
        _drain_tasks_during_cleanup(
            observers.pending_tasks,
            timeout_error,
            message=_PIPELINE_FINALIZATION_ERROR,
        )
    )


async def _finalize_pipeline_stage_result_failure(
    spawn: _PipelineSpawnResult,
    observers: _PipelineObservers,
    sink_bracket: _SinkBracket,
    result_error: BaseException,
) -> None:
    """Settle stages and drain hooks after stage-result assembly fails."""
    outcome = _outcome_for_error(result_error)
    sink_bracket.close(outcome=outcome)
    _emit_terminal_events(
        observers.observations,
        outcome.outcome,
        processes=spawn.processes,
        started_at=spawn.stages.started_at,
    )
    # Result assembly runs after process cleanup but before pipeline
    # finalization, so this layer still owns the observe-hook tasks.
    await _shielded_cleanup(
        _drain_tasks_during_cleanup(
            observers.pending_tasks,
            result_error,
            message=_PIPELINE_FINALIZATION_ERROR,
        )
    )


def _run_pipeline_after_hooks(
    parts: tuple[SafeCmd, ...],
    hooks_by_stage: tuple[_ExecutionHooks, ...],
    results: list[CommandResult],
) -> None:
    """Run registered after hooks for each pipeline stage."""
    for cmd, hooks, result in zip(parts, hooks_by_stage, results, strict=True):
        for hook in hooks.after_hooks:
            hook(cmd, result)


__all__ = [
    "_PIPELINE_FINALIZATION_ERROR",
    "_finalize_pipeline_execution",
    "_finalize_pipeline_run_failure",
    "_finalize_pipeline_stage_result_failure",
    "_finalize_pipeline_timeout",
    "_reconcile_pipeline_run_failure",
    "_run_pipeline_after_hooks",
]
