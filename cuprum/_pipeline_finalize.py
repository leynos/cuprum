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

from cuprum._capture_cleanup import _cleanup_capture_sinks
from cuprum._delivery_lifecycle import _close_delivery_worker
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
    from cuprum._pipeline_config import _PipelineRunConfig
    from cuprum._pipeline_records import (
        _PipelineObservers,
        _PipelineSpawnResult,
    )
    from cuprum._pipeline_types import _ExecutionHooks
    from cuprum._result_types import _AnyCommandResult
    from cuprum.sh import SafeCmd


_PIPELINE_FINALIZATION_ERROR = "pipeline finalization failed"


async def _finalize_pipeline_execution(
    parts: tuple[SafeCmd, ...],
    observers: _PipelineObservers,
    stage_results: list[_AnyCommandResult],
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
        await _shielded_cleanup(sink_bracket.aclose(outcome=outcome))
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
    await _shielded_cleanup(
        sink_bracket.aclose(outcome=_pipeline_result_outcome(stage_results))
    )
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


def _run_pipeline_after_hooks(
    parts: tuple[SafeCmd, ...],
    hooks_by_stage: tuple[_ExecutionHooks, ...],
    results: list[_AnyCommandResult],
) -> None:
    """Run registered after hooks for each pipeline stage."""
    for cmd, hooks, result in zip(parts, hooks_by_stage, results, strict=True):
        for hook in hooks.after_hooks:
            hook(cmd, result)


async def _reconcile_pipeline_run_failure(
    config: _PipelineRunConfig,
    spawn: _PipelineSpawnResult,
    observers: _PipelineObservers,
    run_error: BaseException,
) -> None:
    """Cancel the stream tasks and drain the observe tasks after a run failure.

    Kept as one coroutine so the caller can shield both halves together: the
    stream tasks and the observe-hook tasks are all owned by the pipeline, and
    a cancellation arriving between two separately shielded steps would leave
    the second set pending. The run-owned delivery worker closes before the
    session does, so write failures and undelivered bytes it discovered are
    already recorded when the terminal events are emitted; the session close
    itself is the asynchronous one, because in bounded mode it waits for the
    worker's queue to drain.

    The capture cleanup runs last, after the cancels have been awaited. A
    cancelled consumer is what this path exists for, and it never finalizes, so
    its accumulator is the only remaining owner of the spill file; closing that
    file before the cancel has landed would race a reader that is still
    appending. Every step above it — the delivery close, the session close, and
    the terminal events — runs inside the ``try`` whose ``finally`` holds the
    release, because each of them can raise or, through the shield, re-raise a
    cancellation. Cleanup that only guarded the drain would strand the spill
    files on exactly those failures. The drain's own group, when it raises,
    propagates unchanged.
    """
    outcome = _outcome_for_error(run_error)
    try:
        await _cancel_stream_tasks(spawn.stderr_tasks, spawn.stdout_task)
        await _close_delivery_worker(
            config.delivery_worker,
            drain_grace_s=config.delivery_grace_s,
        )
        await _shielded_cleanup(config.sink_bracket.aclose(outcome=outcome))
        _emit_terminal_events(
            observers.observations,
            outcome.outcome,
            processes=spawn.processes,
            started_at=spawn.stages.started_at,
        )
    finally:
        await _settle_observe_tasks(observers, spawn, run_error)


async def _settle_observe_tasks(
    observers: _PipelineObservers,
    spawn: _PipelineSpawnResult,
    cause: BaseException,
) -> None:
    """Drain the observe tasks, then release the sinks that fed them.

    One coroutine so the caller can shield both halves together. A
    cancellation arriving between two separately shielded steps would leave the
    sinks unclosed and the spill files on disk. The release sits in
    ``finally`` for the same reason: a failing observe task raises out of the
    drain, and that must not become the exit that leaks the file.
    """
    try:
        await _drain_tasks_during_cleanup(
            observers.pending_tasks,
            cause,
            message=_PIPELINE_FINALIZATION_ERROR,
        )
    finally:
        await _cleanup_capture_sinks(spawn.captures.sinks)


async def _finalize_pipeline_run_failure(
    config: _PipelineRunConfig,
    spawn: _PipelineSpawnResult,
    observers: _PipelineObservers,
    run_error: BaseException,
) -> None:
    """Close the sink, settle spawned stages, and drain after run failure."""
    await _shielded_cleanup(
        _reconcile_pipeline_run_failure(
            config,
            spawn,
            observers,
            run_error,
        )
    )


async def _finalize_pipeline_timeout(
    config: _PipelineRunConfig,
    spawn: _PipelineSpawnResult,
    observers: _PipelineObservers,
    timeout_error: BaseException,
) -> None:
    """Report a pipeline timeout and finalize its sink and observe tasks.

    The delivery worker and the presentation session close before the capture
    sinks, because those sinks may still be feeding the worker. Each of those
    closes is shielded, but a shield completes its operation and then re-raises
    the caller's cancellation, so an enclosing ``finally`` is what guarantees
    the capture release still runs when the run is cancelled part-way through
    teardown — the caller's cancellation, or an exception from a session close,
    must not leave this run's spill files on disk.
    """
    observations = observers.observations
    try:
        await _shielded_cleanup(
            _close_delivery_worker(
                config.delivery_worker,
                drain_grace_s=config.delivery_grace_s,
            ),
        )
        await _shielded_cleanup(
            config.sink_bracket.aclose(outcome=_outcome_for_error(timeout_error))
        )
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
    finally:
        await _shielded_cleanup(_settle_observe_tasks(observers, spawn, timeout_error))


async def _finalize_pipeline_stage_result_failure(
    spawn: _PipelineSpawnResult,
    observers: _PipelineObservers,
    sink_bracket: _SinkBracket,
    result_error: BaseException,
) -> None:
    """Settle stages and drain hooks after stage-result assembly fails.

    The capture sinks are released from a ``finally`` that encloses the
    session close and the terminal events for the same reason as the timeout
    path: a shielded close that re-raises a cancellation, or a session close
    that raises, must not bypass the one step that removes this run's spill
    files.
    """
    outcome = _outcome_for_error(result_error)
    try:
        await _shielded_cleanup(sink_bracket.aclose(outcome=outcome))
        _emit_terminal_events(
            observers.observations,
            outcome.outcome,
            processes=spawn.processes,
            started_at=spawn.stages.started_at,
        )
    finally:
        # Result assembly runs after process cleanup but before pipeline
        # finalization, so this layer still owns the observe-hook tasks.
        await _shielded_cleanup(_settle_observe_tasks(observers, spawn, result_error))


__all__ = [
    "_PIPELINE_FINALIZATION_ERROR",
    "_finalize_pipeline_execution",
    "_finalize_pipeline_run_failure",
    "_finalize_pipeline_stage_result_failure",
    "_finalize_pipeline_timeout",
    "_reconcile_pipeline_run_failure",
    "_run_pipeline_after_hooks",
]
