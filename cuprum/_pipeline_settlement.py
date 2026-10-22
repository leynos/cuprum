"""Bounded observation and settlement of a pipeline's output consumers.

A stage's exit does not close its pipes: a descendant that inherited them keeps
every reader blocked long after the stage itself is gone. This module owns the
one place a pipeline observes its readers under the configured deadline and
then settles them, so neither route that can reach an expiry — the stage wait
timing out, or the final gather failing to finish — grows its own copy of that
rule.

Observation and settlement are deliberately separate steps. Observation only
*reports* that readers are still unfinished; settlement is what cancels them,
and it runs after the stages have been terminated so a reader can still capture
what its stage had already written. It collaborates with
``cuprum._pipeline_stream_results`` for the deadline-aware wait and the
consumer settlement itself, with ``cuprum._pipeline_teardown`` for the stage
termination, and with ``cuprum._process_lifecycle`` for the shield that keeps a
cancellation from tearing the settlement in half. It answers with what the run
owes; deciding how to report that is the caller's job.
"""

from __future__ import annotations

import typing as typ

from cuprum._pipeline_records import (
    _PipelineOutputGather,
    _PipelineOutputs,
    _PipelineSpawnResult,
)
from cuprum._pipeline_stream_results import (
    _bounded_payloads,
    _reconcile_pipe_tasks,
    _settle_pipeline_outputs,
)
from cuprum._pipeline_teardown import _terminate_timed_out_stages
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._subprocess_wait_types import _DrainContext
from cuprum._teardown_policy import _TeardownPolicy

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._pipeline_config import _PipelineRunConfig
    from cuprum._stream_drain_finish import _CapturedDrain
    from cuprum._subprocess_wait_types import _StreamConsumerTask
    from cuprum.echo_events import RelayFallback


def _pipeline_output_tasks(
    spawn: _PipelineSpawnResult,
) -> list[_StreamConsumerTask | None]:
    """List the spawn's consumers as stderr by stage, then final stdout."""
    return [*spawn.stderr_tasks, spawn.stdout_task]


async def _gather_pipeline_outputs(
    spawn: _PipelineSpawnResult,
    *,
    deadline: float | None,
) -> _PipelineOutputGather:
    """Observe the pipeline's readers without waiting past an absolute deadline.

    A stage's exit does not close its pipes: a descendant that inherited them
    keeps every reader blocked long after the stage itself is gone, so an
    unbounded gather would let that descendant outlive the caller's configured
    ``timeout=``. The budget is what ends the observation; readers still
    running when it expires are reported as unfinished rather than cancelled
    here.

    Cancelling them is deliberately left to the caller, and not merely for
    tidiness: on the expiry route the stages are terminated *first*, which is
    what releases the pipes and lets a reader capture what its stage had
    already written. A cancel issued here would close that window.

    Returns
    -------
    _PipelineOutputGather
        The consumers awaited, what each had produced, and whether every one
        finished.
    """
    tasks = _pipeline_output_tasks(spawn)
    drains, finished = await _bounded_payloads(tasks, deadline=deadline)
    return _PipelineOutputGather(tasks=tasks, drains=drains, finished=finished)


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


def _drain_context(
    spawn: _PipelineSpawnResult,
    config: _PipelineRunConfig,
) -> _DrainContext:
    """Build the drain context for settling the pipeline's consumers.

    The resolved policies travel per stream, in the stdout-then-stderr order
    every drain context uses, because the empty result a lost reader owes its
    caller is built from its *own* stream's policy: a pipeline that captured
    only stderr must not report an empty capture for stdout.

    The pending-reader count it feeds is run-wide, so the telemetry identity is
    the final stage's: it names the run the caller awaits rather than whichever
    reader happened to be stuck.

    Returns
    -------
    _DrainContext
        The per-stream capture policies, mode fallback, and telemetry identity
        for settlement.
    """
    observations = spawn.stages.observations
    observation = observations[-1] if observations else None
    process = spawn.processes[-1] if spawn.processes else None
    return _DrainContext(
        captures=(config.capture_stdout, config.capture_stderr),
        pid=None if process is None else process.pid,
        observation=observation,
        capture_bytes=config.capture_bytes,
    )


async def _settle_gathered_outputs(
    gathered: _PipelineOutputGather,
    *,
    spawn: _PipelineSpawnResult,
    config: _PipelineRunConfig,
) -> tuple[tuple[_CapturedDrain | None, ...], _CapturedDrain | None]:
    """Settle every consumer under the shield, as one unit.

    Shielded because settlement is itself a gather: a cancellation landing on
    it would cancel the consumers without waiting for them.

    Returns
    -------
    tuple[tuple[_CapturedDrain | None, ...], _CapturedDrain | None]
        Per-stage stderr, then the final stage's stdout, each carrying the
        record describing how its stream was retained.
    """
    return await _shielded_cleanup(
        _settle_pipeline_outputs(
            gathered.drains,
            gathered.tasks,
            context=_drain_context(spawn, config),
            expired=not gathered.finished,
        ),
    )


def _unobserved_pipeline_outputs(
    spawn: _PipelineSpawnResult,
) -> _PipelineOutputGather:
    """Report the pipeline's readers as unobserved, for settlement to await.

    The expiry route has no observation to make. Both ways it is reached have
    already spent the run's budget — the stage wait consumed it, or the final
    gather did — so a deadline-bound gather here would be handed a deadline
    that has passed and could only report every reader unfinished. Awaiting
    one anyway would also give a reader's own failure the chance to replace the
    expiry the caller is owed, which is the opposite of the rule the success
    route applies: there the failure ends the run, so it must be reported,
    while here the run has already ended and settlement is what reads the
    pipes.

    What this reports instead is the same reader list with nothing observed,
    so :func:`_settle_gathered_outputs` supplies the bounded EOF grace, cancels
    whatever is left, and reads every payload from the settled tasks. The
    ``finished=False`` is not a claim that a reader is stuck; it is what asks
    a capturing settlement for that grace.

    Returns
    -------
    _PipelineOutputGather
        The readers, an absent observation for each, and a settlement that owes
        the EOF grace.
    """
    tasks = _pipeline_output_tasks(spawn)
    return _PipelineOutputGather(
        tasks=tasks,
        drains=(None,) * len(tasks),
        finished=False,
    )


async def _expired_pipeline_outputs(
    spawn: _PipelineSpawnResult,
    config: _PipelineRunConfig,
    *,
    pipe_tasks: list[asyncio.Task[None]],
) -> _PipelineOutputs:
    """End an expired run and report what its readers had captured.

    Both routes that can discover an expiry call this one sequence, so the
    wait-timeout route and the final-output expiry cannot drift apart. The
    order is what makes the run endable: the stages are terminated first —
    including an owned group whose leader has already exited, since it is the
    group that holds the pipes — the inter-stage pumps are reconciled so no
    relay writes into a reader about to be cancelled, and only then are the
    readers settled.

    No deadline is passed on, because there is none left to respect: see
    :func:`_unobserved_pipeline_outputs`. The bound the caller configured has
    already expired, and what remains is the bounded settlement that reads the
    pipes the terminated stages have released.

    Returns
    -------
    _PipelineOutputs
        Whatever the terminated stages had already produced, in the mode the
        run captured, with each stage's relay fallbacks.
    """
    await _terminate_timed_out_stages(
        spawn.processes,
        _TeardownPolicy(config.ctx.cancel_grace, owns_group=spawn.owns_group),
        observations=spawn.stages.observations,
    )
    await _reconcile_pipe_tasks(pipe_tasks)
    stderr_by_stage, final_stdout = await _settle_gathered_outputs(
        _unobserved_pipeline_outputs(spawn),
        spawn=spawn,
        config=config,
    )
    return _PipelineOutputs(
        stderr_by_stage=stderr_by_stage,
        final_stdout=final_stdout,
        capture_stdout=config.capture_stdout,
        capture_stderr=config.capture_stderr,
        capture_bytes=config.capture_bytes,
        relay_fallbacks_by_stage=_stage_relay_fallbacks(spawn),
    )
