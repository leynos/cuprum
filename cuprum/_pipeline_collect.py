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
from cuprum._pipeline_records import (
    _PipelineOutputs,
    _PipelineSpawnResult,
    _PipelineStageResultInputs,
)
from cuprum._pipeline_stream_results import (
    _gather_optional_text_tasks,
    _reconcile_pipe_tasks,
)
from cuprum._pipeline_streams import _create_pipe_tasks
from cuprum._pipeline_teardown import _terminate_timed_out_stages
from cuprum._pipeline_types import _ExecutionInvariantError
from cuprum._pipeline_wait import _wait_for_pipeline
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._result_assembly import _require_bytes, _require_text
from cuprum._teardown_policy import _TeardownPolicy

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import types

    from cuprum._pipeline_config import _PipelineRunConfig
    from cuprum._pipeline_wait import _PipelineWaitResult
    from cuprum._stream_drain_finish import _CapturedDrain
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
        stages=spawn.stages,
        policy=_TeardownPolicy(
            config.ctx.cancel_grace,
            owns_group=spawn.owns_group,
        ),
    )
    try:
        if wait_timeout is None:
            return await pipeline_wait
        return await asyncio.wait_for(pipeline_wait, wait_timeout)
    finally:
        await _shielded_cleanup(_stop_idle_monitor(spawn.idle))


async def _gather_pipeline_outputs(
    spawn: _PipelineSpawnResult,
) -> tuple[tuple[_CapturedDrain | None, ...], _CapturedDrain | None]:
    """Gather stderr by stage and final stdout from spawn tasks.

    Each drain result carries its payload in the pipeline's mode — text in the
    ordinary one, the child's bytes untouched in the byte-exact one — and it
    carries the capture record and any spill handle with it. Nothing here
    inspects either; the mode is read once, where each result is built, and the
    record travels on to that same site because it is the only thing that knows
    which spill file this run still has to remove.

    Returns
    -------
    tuple[tuple[_CapturedDrain | None, ...], _CapturedDrain | None]
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


def _join_stage_stderr(
    stderr_by_stage: tuple[_CapturedDrain | None, ...],
    *,
    capture_bytes: bool,
) -> _StreamPayload:
    """Join each stage's stderr in stage order, in the pipeline's mode.

    Only called for a capturing run, so every chunk is present in the mode the
    config carried. The join cannot be shared across modes: ``bytes.join`` and
    ``str.join`` are different methods, and narrowing a generator to match a
    dynamically chosen empty value is not something a type checker can follow.
    Each branch therefore stays separate, and each narrows the chunks it joins,
    because a stage payload in the wrong mode is an internal contradiction —
    the helpers that say so are the same ones the result builders use.

    A stage with no consumer contributes nothing, which is the same empty value
    the join would otherwise supply: a stage whose stderr was never piped has
    no stderr to report, and the deadline path must not invent one.

    Returns
    -------
    _StreamPayload
        The concatenated stderr. An all-empty byte-exact run is ``b""``, not
        ``None``: ``b""`` is itself a valid join result, and the distinction
        between it and ``None`` is the one the capture flag draws everywhere.
    """
    payloads = tuple(_stage_stderr_payloads(stderr_by_stage))
    if capture_bytes:
        return b"".join(_require_bytes(chunk, "stderr") or b"" for chunk in payloads)
    return "".join(_require_text(chunk, "stderr") or "" for chunk in payloads)


def _stage_stderr_payloads(
    stderr_by_stage: tuple[_CapturedDrain | None, ...],
) -> cabc.Iterator[_StreamPayload | None]:
    """Yield each stage's captured stderr payload, in stage order."""
    for drain in stderr_by_stage:
        yield None if drain is None else drain.payload


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
    stderr_text: _StreamPayload | None = None
    if outputs.capture_stderr is not None:
        stderr_text = _join_stage_stderr(
            outputs.stderr_by_stage,
            capture_bytes=outputs.capture_bytes,
        )
    return _sh_module().TimeoutExpired(
        cmd=tuple(cmd.argv_with_program for cmd in parts),
        timeout=timeout,
        output=_final_stage_stdout(outputs),
        stderr=stderr_text,
    )


def _final_stage_stdout(outputs: _PipelineOutputs) -> _StreamPayload | None:
    """Report the final stage's stdout as the expired run's captured output.

    Two independent conditions have to hold, which is why this is a helper
    rather than an inline conditional now that capture is per stream: the
    caller must have asked for stdout at all — a run that captured only stderr
    reports no ``output``, exactly as it reports no ``stdout`` on its stage
    result — and the spawn must have attached a stdout consumer. The payload
    is then narrowed to the run's mode, because the widening that lets one
    drain serve both modes leaves the type a type checker cannot narrow here.

    Returns
    -------
    _StreamPayload | None
        The child's captured stdout in the run's mode, or ``None`` when
        stdout was not captured or no consumer read it.
    """
    drain = outputs.final_stdout
    if outputs.capture_stdout is None or drain is None:
        return None
    if outputs.capture_bytes:
        return _require_bytes(drain.payload, "stdout")
    return _require_text(drain.payload, "stdout")


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
        await _terminate_timed_out_stages(
            spawn.processes,
            _TeardownPolicy(
                config.ctx.cancel_grace,
                owns_group=spawn.owns_group,
            ),
            observations=spawn.stages.observations,
        )
        await _reconcile_pipe_tasks(pipe_tasks)
        stderr_by_stage, final_stdout = await _gather_pipeline_outputs(spawn)
        relay_fallbacks_by_stage = _stage_relay_fallbacks(spawn)
        if timeout is None:
            msg = "TimeoutError without a configured timeout"
            raise _PipelineInvariantError(msg) from exc
        outputs = _PipelineOutputs(
            stderr_by_stage=stderr_by_stage,
            final_stdout=final_stdout,
            capture_stdout=config.capture_stdout,
            capture_stderr=config.capture_stderr,
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
