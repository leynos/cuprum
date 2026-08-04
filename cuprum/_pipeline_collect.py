"""Pipeline completion waiting and output collection.

This module holds the private machinery that drives a spawned pipeline
to completion and gathers its captured output. It waits for every stage
to exit (honouring an optional timeout deadline), collects per-stage
stderr and the final stage's stdout, and maps a timeout into a
``TimeoutExpired`` carrying whatever output was captured. It also hosts
the ``cuprum.sh`` lazy-import shim used to build those results. It is a
companion to ``cuprum._pipeline_internals``, which re-exports its names
to preserve its public surface, and collaborates with
``cuprum._pipeline_streams``, ``cuprum._pipeline_types``, and
``cuprum._pipeline_wait``.
"""

from __future__ import annotations

import asyncio
import sys
import time
import typing as typ

from cuprum._pipeline_streams import (
    _create_pipe_tasks,
    _gather_optional_text_tasks,
    _PipelineRunConfig,
)
from cuprum._pipeline_types import (
    _ExecutionInvariantError,
    _PipelineOutputs,
    _PipelineSpawnResult,
    _PipelineStageResultInputs,
)
from cuprum._pipeline_wait import _wait_for_pipeline

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import types

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
    clock: cabc.Callable[[], float] = time.monotonic,
) -> _PipelineWaitResult:
    """Wait for the pipeline to finish, honouring any timeout deadline."""
    wait_timeout: float | None = None
    if timeout_deadline is not None:
        wait_timeout = max(0.0, timeout_deadline - clock())
    pipeline_wait = _wait_for_pipeline(
        spawn.processes,
        pipe_tasks=_create_pipe_tasks(spawn.processes),
        cancel_grace=config.ctx.cancel_grace,
        started_at=spawn.started_at,
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

    # ``no-else-raise`` treats the ``else`` as removable because the handler
    # raises, but here it is load-bearing: it keeps the output gathering out of
    # the ``try`` so ``except TimeoutError`` covers only the wait, and a timeout
    # raised while gathering is not misreported as a pipeline timeout.
    try:  # pylint: disable=no-else-raise
        wait_result = await _await_pipeline_wait_result(
            spawn,
            config,
            timeout_deadline=timeout_deadline,
        )
    except TimeoutError as exc:
        stderr_by_stage, final_stdout = await _gather_pipeline_outputs(spawn)
        if timeout is None:
            msg = "TimeoutError without a configured timeout"
            raise _PipelineInvariantError(msg) from exc
        outputs = _PipelineOutputs(
            stderr_by_stage=stderr_by_stage,
            final_stdout=final_stdout,
            capture=config.capture,
        )
        raise _build_timeout_expired_error(parts, timeout, outputs) from exc
    else:
        stderr_by_stage, final_stdout = await _gather_pipeline_outputs(spawn)
        return _PipelineStageResultInputs(
            wait_result=wait_result,
            stderr_by_stage=stderr_by_stage,
            final_stdout=final_stdout,
        )
