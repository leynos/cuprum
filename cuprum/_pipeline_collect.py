"""Pipeline completion waiting and output collection.

This module holds the private machinery that drives a spawned pipeline
to completion and gathers its captured output. It waits for every stage
to exit (honouring an optional timeout deadline), collects per-stage
stderr and the final stage's stdout, and maps a timeout into a
``TimeoutExpired`` carrying whatever output was captured. It also hosts
the ``cuprum.sh`` lazy-import shim used to build those results. It is a
companion to ``cuprum._pipeline_internals``, which re-exports its names
to preserve its public surface, and collaborates with
``cuprum._pipeline_streams``, ``cuprum._pipeline_types``,
``cuprum._pipeline_wait``, and ``cuprum._process_lifecycle``.
"""

from __future__ import annotations

import asyncio
import sys
import time
import typing as typ

from cuprum._pipeline_stream_results import (
    _gather_optional_text_tasks,
    _reconcile_pipe_tasks,
)
from cuprum._pipeline_streams import _create_pipe_tasks
from cuprum._pipeline_types import (
    _ExecutionInvariantError,
    _PipelineOutputs,
    _PipelineSpawnResult,
    _PipelineStageResultInputs,
)
from cuprum._pipeline_wait import _wait_for_pipeline
from cuprum._process_lifecycle import (
    _shielded_cleanup,
    _terminate_timed_out_stages,
)

if typ.TYPE_CHECKING:
    import types

    from cuprum._pipeline_config import _PipelineRunConfig
    from cuprum._pipeline_wait import _PipelineWaitResult
    from cuprum.sh import SafeCmd


class _PipelineInvariantError(_ExecutionInvariantError):
    """Raised when an internal pipeline-execution invariant is violated.

    Subclasses the shared package-level invariant error, which itself derives
    from :class:`RuntimeError`, while retaining a distinct type for pipeline
    failures. Mirrors
    :class:`cuprum._subprocess_timeout._SubprocessInvariantError` for the
    single-command path.
    """


def _sh_module() -> types.ModuleType:
    """Return the imported ``cuprum.sh`` module or raise if it is absent."""
    module = sys.modules.get("cuprum.sh")
    if module is None:
        msg = "cuprum.sh must be imported before running pipelines"
        raise _PipelineInvariantError(msg)
    return module


async def _await_pipeline_wait_result(
    spawn: _PipelineSpawnResult,
    config: _PipelineRunConfig,
    *,
    timeout_deadline: float | None,
    pipe_tasks: list[asyncio.Task[None]],
) -> _PipelineWaitResult:
    """Wait for the pipeline to finish, honouring any timeout deadline.

    ``pipe_tasks`` belongs to the caller: a non-positive deadline cancels
    ``_wait_for_pipeline`` before the ``finally`` that would reconcile them,
    so the caller reconciles them instead (see the developers' guide).

    Returns
    -------
    _PipelineWaitResult
        The result of waiting on the pipeline's stage processes, as
        produced by ``_wait_for_pipeline``.
    """
    wait_timeout: float | None = None
    if timeout_deadline is not None:
        wait_timeout = max(0.0, timeout_deadline - time.monotonic())
    pipeline_wait = _wait_for_pipeline(
        spawn.processes,
        pipe_tasks=pipe_tasks,
        cancel_grace=config.ctx.cancel_grace,
        stages=spawn.stages,
    )
    if wait_timeout is None:
        return await pipeline_wait
    return await asyncio.wait_for(pipeline_wait, wait_timeout)


async def _gather_pipeline_outputs(
    spawn: _PipelineSpawnResult,
) -> tuple[tuple[str | None, ...], str | None]:
    """Gather stderr by stage and final stdout from spawn tasks."""
    stderr_by_stage = await _gather_optional_text_tasks(spawn.stderr_tasks)
    final_stdout = None if spawn.stdout_task is None else await spawn.stdout_task
    return stderr_by_stage, final_stdout


def _stage_relay_fallbacks(
    spawn: _PipelineSpawnResult,
) -> tuple[tuple[RelayFallback, ...], ...]:
    """Read each stage's relay diagnostics from its own collectors.

    Every stage's tuple lists its stdout records first (final stage only,
    matching the single-command result order) and then its stderr records.
    Unsettled collectors — a drain cancelled during teardown — contribute an
    empty tuple, keeping those diagnostics on the echo observation channel.
    """
    stage_tuples: list[tuple[RelayFallback, ...]] = []
    for stderr_diagnostics, stdout_diagnostics in spawn.relay_diagnostics_by_stage:
        if stdout_diagnostics is not None:
            stdout_diagnostics.settle()
        if stderr_diagnostics is not None:
            stderr_diagnostics.settle()
        stdout_fallbacks = (
            () if stdout_diagnostics is None else stdout_diagnostics.snapshot()
        )
        stderr_fallbacks = (
            () if stderr_diagnostics is None else stderr_diagnostics.snapshot()
        )
        stage_tuples.append(stdout_fallbacks + stderr_fallbacks)
    return tuple(stage_tuples)


def _build_timeout_expired_error(
    parts: tuple[SafeCmd, ...],
    timeout: float,
    outputs: _PipelineOutputs,
) -> BaseException:
    """Construct a TimeoutExpired exception with captured outputs."""
    stderr_text = None
    if outputs.capture:
        stderr_text = "".join(text or "" for text in outputs.stderr_by_stage)
    output = outputs.final_stdout if outputs.capture else None
    return _sh_module().TimeoutExpired(
        cmd=tuple(cmd.argv_with_program for cmd in parts),
        timeout=timeout,
        output=output,
        stderr=stderr_text,
    )


async def _collect_pipeline_inputs(
    parts: tuple[SafeCmd, ...],
    spawn: _PipelineSpawnResult,
    config: _PipelineRunConfig,
) -> _PipelineStageResultInputs:
    """Await pipeline completion and collect outputs, mapping timeouts."""
    timeout = config.timeout
    timeout_deadline: float | None = None
    if timeout is not None:
        timeout_deadline = time.monotonic() + timeout

    pipe_tasks = _create_pipe_tasks(
        spawn.processes,
        observations=spawn.stages.observations,
    )
    try:
        wait_result = await _await_pipeline_wait_result(
            spawn,
            config,
            timeout_deadline=timeout_deadline,
            pipe_tasks=pipe_tasks,
        )
    except TimeoutError as exc:
        await _terminate_timed_out_stages(spawn.processes, config.ctx.cancel_grace)
        await _reconcile_pipe_tasks(pipe_tasks)
        stderr_by_stage, final_stdout = await _gather_pipeline_outputs(spawn)
        relay_fallbacks_by_stage = _stage_relay_fallbacks(spawn)
        if timeout is None:
            msg = "TimeoutError without a configured timeout"
            raise _PipelineInvariantError(msg) from exc
        outputs = _PipelineOutputs(
            stderr_by_stage=stderr_by_stage,
            final_stdout=final_stdout,
            capture=config.capture,
            relay_fallbacks_by_stage=relay_fallbacks_by_stage,
        )
        raise _build_timeout_expired_error(parts, timeout, outputs) from exc
    finally:
        # The inter-stage pumps are owned here, so every exit reconciles them:
        # success, the deadline path, caller cancellation, and the non-positive
        # deadline that cancels _wait_for_pipeline before its own finally can
        # run. The timeout branch above still reconciles explicitly, because
        # the pumps must reach EOF after the stages are terminated but *before*
        # the outputs are gathered; _reconcile_pipe_tasks is safe to run twice,
        # so this is a no-op once that has happened. Shielded because the
        # reconciliation is itself a gather: a cancellation landing on it would
        # cancel the pumps without waiting for them to settle, leaving the
        # tasks this function owns running detached with nobody left to await
        # them.
        await _shielded_cleanup(_reconcile_pipe_tasks(pipe_tasks))

    # Gathering sits after the ``try`` so ``except TimeoutError`` covers only
    # the wait: a timeout raised while gathering is a different failure and must
    # not be reported as a pipeline timeout. Every branch of the handler raises,
    # so this is reached only on success.
    stderr_by_stage, final_stdout = await _gather_pipeline_outputs(spawn)
    return _PipelineStageResultInputs(
        wait_result=wait_result,
        stderr_by_stage=stderr_by_stage,
        final_stdout=final_stdout,
        relay_fallbacks_by_stage=_stage_relay_fallbacks(spawn),
    )
