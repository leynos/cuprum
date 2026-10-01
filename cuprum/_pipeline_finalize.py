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
from cuprum._pipeline_results import _emit_timeout_exit_events
from cuprum._pipeline_sink import _pipeline_result_outcome
from cuprum._pipeline_stream_results import _cancel_stream_tasks
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._sink_lifecycle import _outcome_for_error, _SinkBracket
from cuprum._timeout_reporting import _report_pipeline_timeout_expiry

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._pipeline_config import _PipelineRunConfig
    from cuprum._pipeline_types import (
        _ExecutionHooks,
        _PipelineObservers,
        _PipelineSpawnResult,
    )
    from cuprum._result_types import _AnyCommandResult
    from cuprum.sh import SafeCmd


_PIPELINE_FINALIZATION_ERROR = "pipeline finalization failed"


async def _finalize_pipeline_execution(
    parts: tuple[SafeCmd, ...],
    observers: _PipelineObservers,
    stage_results: list[_AnyCommandResult],
    sink_bracket: _SinkBracket,
) -> None:
    """Run after hooks, commit the sink outcome, then drain observe tasks.

    This is the pipeline's counterpart to the command path's
    :func:`cuprum._command_internals._execute_with_hooks`, and the three steps
    are in that order for the same reason: an after-hook that raises is a
    terminal *run* error, so the outcome the adapter records has to be decided
    after the hooks have had their say. Closing with the stage-result outcome
    first would leave the adapter reporting ``exit_zero`` for a run that ended
    in an exception, and :meth:`_SinkBracket.close` clears its session on the
    first call, so the error close that followed would be a no-op.

    Both drains are shielded. The pipeline owns these observe-hook tasks, so a
    cancellation landing while finalization waits on them must not return
    before they have settled — that would leak a task per pending hook.
    """
    observations = observers.observations
    pending_tasks = observers.pending_tasks
    hooks_by_stage = tuple(obs.hooks for obs in observations)
    try:
        _run_pipeline_after_hooks(parts, hooks_by_stage, stage_results)
    except BaseException as after_hook_error:
        sink_bracket.close(outcome=_outcome_for_error(after_hook_error))
        await _shielded_cleanup(
            _drain_tasks_during_cleanup(
                pending_tasks, after_hook_error, message=_PIPELINE_FINALIZATION_ERROR
            )
        )
        raise
    sink_bracket.close(outcome=_pipeline_result_outcome(stage_results))
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
    spawn: _PipelineSpawnResult,
    pending_tasks: list[asyncio.Task[None]],
    run_error: BaseException,
) -> None:
    """Cancel the stream tasks and drain the observe tasks after a run failure.

    Kept as one coroutine so the caller can shield both halves together: the
    stream tasks and the observe-hook tasks are all owned by the pipeline, and
    a cancellation arriving between two separately shielded steps would leave
    the second set pending.
    """
    await _cancel_stream_tasks(spawn.stderr_tasks, spawn.stdout_task)
    await _drain_tasks_during_cleanup(
        pending_tasks, run_error, message=_PIPELINE_FINALIZATION_ERROR
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
    await _shielded_cleanup(
        _drain_tasks_during_cleanup(
            observers.pending_tasks,
            timeout_error,
            message=_PIPELINE_FINALIZATION_ERROR,
        )
    )


__all__ = [
    "_finalize_pipeline_execution",
    "_finalize_pipeline_timeout",
    "_reconcile_pipeline_run_failure",
    "_run_pipeline_after_hooks",
]
