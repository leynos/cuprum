"""Pipeline completion waiting and output collection.

This module holds the private machinery that drives a spawned pipeline
to completion and gathers its captured output. It waits for every stage
to exit (honouring an optional timeout deadline), collects per-stage
stderr and the final stage's stdout, and maps a timeout into a
``TimeoutExpired`` carrying whatever output was captured. It also hosts
the ``cuprum.sh`` lazy-import shim used to build those results. It is a
companion to ``cuprum._pipeline_internals``, which re-exports its names
to preserve its public surface, and collaborates with
``cuprum._pipeline_spawn``, ``cuprum._pipeline_streams``,
``cuprum._pipeline_types``, ``cuprum._pipeline_wait``, and
``cuprum._process_lifecycle``.
"""

from __future__ import annotations

import asyncio
import sys
import time
import typing as typ

from cuprum._idle_heartbeat import _stop_idle_monitor
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
from cuprum._result_assembly import _require_bytes, _require_text

if typ.TYPE_CHECKING:
    import types

    from cuprum._pipeline_config import _PipelineRunConfig
    from cuprum._pipeline_wait import _PipelineWaitResult
    from cuprum._subprocess_wait_types import _StreamPayload
    from cuprum.echo_events import RelayFallback
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

    The run's idle heartbeat is stopped here as well. Once every stage has
    settled -- or a deadline is taking over their teardown -- the pipeline is no
    longer "still running", and the output gathering that follows can be held
    open by a grandchild's inherited pipe long after the stages are gone.

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
    try:
        if wait_timeout is None:
            return await pipeline_wait
        return await asyncio.wait_for(pipeline_wait, wait_timeout)
    finally:
        await _shielded_cleanup(_stop_idle_monitor(spawn.idle))


async def _gather_pipeline_outputs(
    spawn: _PipelineSpawnResult,
) -> tuple[tuple[_StreamPayload | None, ...], _StreamPayload | None]:
    """Gather stderr by stage and final stdout from spawn tasks.

    Each payload arrives in the pipeline's mode: text in the ordinary one, the
    child's bytes untouched in the byte-exact one. Nothing here inspects the
    type; the mode is read once, where each result is built.

    Returns
    -------
    tuple[tuple[_StreamPayload | None, ...], _StreamPayload | None]
        Each stage's stderr in stage order, then the final stage's stdout, or
        ``None`` where the spawn attached no such consumer.
    """
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

    Returns
    -------
    tuple[tuple[RelayFallback, ...], ...]
        One tuple per stage, in stage order, each listing that stage's stdout
        records first (final stage only) and then its stderr records.
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
    """Construct a TimeoutExpired exception with captured outputs.

    The stage stderr streams are joined in stage order, and the join has to
    be built in the pipeline's mode: a byte-exact pipeline concatenates its
    bytes under ``b""``, because joining bytes under a text separator would
    raise rather than report the partial output. ``b""`` is itself a valid
    join result, so an all-empty byte-exact run reports an empty byte string
    rather than ``None`` — the same distinction the capture flag draws on
    the success path.

    Returns
    -------
    BaseException
        The ``TimeoutExpired`` to raise, carrying whatever partial output the
        terminated stages had produced.
    """
    # Branched rather than joining under a mode-selected separator: the two
    # joins are different methods on different types, and narrowing the
    # generator to match a dynamically chosen empty value is not something a
    # type checker can follow. The duplication is one short expression and
    # buys both branches an honest type. Each branch also narrows the chunks
    # it joins: a stage payload in the wrong mode is an internal contradiction,
    # and the helpers that say so are the same ones the result builders use.
    stderr_text: _StreamPayload | None = None
    if outputs.capture:
        if outputs.capture_bytes:
            stderr_text = b"".join(
                _require_bytes(chunk, "stderr") or b""
                for chunk in outputs.stderr_by_stage
            )
        else:
            stderr_text = "".join(
                _require_text(chunk, "stderr") or ""
                for chunk in outputs.stderr_by_stage
            )
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
        native_pump_cleanup_grace=config.ctx.native_pump_cleanup_grace,
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
            capture_bytes=config.capture_bytes,
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
