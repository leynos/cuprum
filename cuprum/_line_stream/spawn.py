"""Start, build, and on failure unwind one ``SafeCmd.lines()`` run.

Part of the ``cuprum._line_stream`` package. This module owns the pieces that
exist before the run is handed back to its caller: spawning the child, the stdin
writer, the stream consumers, the chained line hooks that feed both the caller's
callback and the queue, and the teardown of a run that failed before it could be
returned.

:func:`_start_line_stream_run` is here rather than in the coordinator because
its whole job is to *reach* the hand-back boundary; the three builders it drives
are defined in this module, so it reads them from here and nothing has to be
re-exported across a module boundary for the step to resolve.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._line_hook_chaining import _chain_line_hooks
from cuprum._line_stream.line_queue import (
    _LineStreamRun,
    _observed_line_hook,
    _queue_line_sink,
)
from cuprum._line_stream.telemetry import _LineStreamTelemetry
from cuprum._pipeline_types import _EventDetails
from cuprum._process_lifecycle import _terminate_all_shielded
from cuprum._subprocess_spawn import _spawn_subprocess
from cuprum._subprocess_streams import (
    _build_run_task_ownership,
    _prepare_stream_spawn,
)
from cuprum._teardown_policy import _TeardownPolicy
from cuprum.line_stream_events import LineStreamPhase

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._line_stream.line_queue import _LineQueueItem
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.lines import LineEvent, _LineHookFn

__all__ = [
    "_abandon_unstarted_run",
    "_build_unstarted_run",
    "_start_line_stream_run",
    "_with_line_sink_hooks",
]


@dc.dataclass(frozen=True, slots=True)
class _LineStreamSetup:
    """Share the queue, telemetry and settlement boundary for one run."""

    queue: asyncio.Queue[_LineQueueItem]
    telemetry: _LineStreamTelemetry
    settlement_started: asyncio.Event
    discard_on_cancel: asyncio.Event


async def _start_line_stream_run(
    execution: _SubprocessExecution,
    queue: asyncio.Queue[_LineQueueItem],
) -> _LineStreamRun:
    """Spawn the subprocess and its queue-feeding consumers.

    ``execution`` must already be authorized for the context in force, with
    its timeout resolved: the caller refuses a forbidden command before it
    builds a queue or a task, and :func:`_refresh_line_stream_execution`
    replaces the timeout the stream was prepared with. This function re-checks
    neither, so it cannot be reached with a stale record by accident.

    Mirrors the streamed-path setup in :func:`_run_subprocess_with_streams`:
    spawn, record the start reference, start the stdin writer, then start the
    consumers with capture and echo as configured.

    Every step between the spawn and the return is owned by
    :func:`_abandon_unstarted_run`, matching :func:`_spawn_pipeline_processes`.
    The ``start`` event is not exempt: it invokes synchronous observe hooks
    inline and re-raises their failures, so without that ownership a failing
    ``start`` hook leaves the child running with undrained pipes, which a caller
    that only drains observe tasks can never reclaim.

    Returns
    -------
    _LineStreamRun
        The spawned run, with the process, its task ownership, the queue,
        and the monotonic start reference.

    Whatever that setup raises propagates unchanged, once the child has been
    stopped and reaped and its tasks drained.
    """
    telemetry = _LineStreamTelemetry(
        exec_id=execution.observation.exec_id,
        queue_capacity=queue.maxsize,
    )
    setup = _LineStreamSetup(queue, telemetry, asyncio.Event(), asyncio.Event())
    # Rebound, not passed straight to the spawn: the stream consumers read
    # ``on_line`` off the execution too, so a chained hook that only reached
    # ``_spawn_subprocess`` would leave the queue sink uninstalled and starve
    # the iterator.
    execution = _with_line_sink_hooks(execution, setup)
    # Sampled *before* the spawn await, matching ``_sample_run_start`` on the
    # direct path. ``started_at`` is the reference every per-line ``at`` stamp
    # is measured against and the exit event's ``started_at`` is published
    # from, so a reading taken afterwards would report a run shorter than the
    # time it actually took — by however long the spawn blocked. Stamping the
    # bundle reads no clock, so doing it first does not disturb the ordering
    # this reference exists to establish.
    # Read from the observation rather than a module-level clock, because the
    # post-exit deadline subtracts it from that same injectable reading: a
    # start taken from a different clock would make the subtraction the
    # difference between two clocks rather than a duration.
    # Rebuilt, not mutated: the stream consumers read ``started_at`` off the
    # execution when stamping each ``LineEvent``. Left at its ``0.0`` default,
    # every ``at`` would be the machine's monotonic uptime rather than seconds
    # since this command started.
    started_at = execution.observation.monotonic_clock()
    execution = dc.replace(execution, started_at=started_at)
    process = await _spawn_subprocess(execution)
    pid = process.pid
    run = _build_unstarted_run(process, execution, setup)
    try:
        if execution.idle is not None:
            # Armed here, once the child is running, exactly as the streamed
            # ``run()`` path arms it: the catalogue checks and before hooks that
            # preceded this spawn are the parent's work, not the child's
            # silence.
            execution.idle.launch()
        telemetry.pid = pid
        # Emitted after the consumers exist, which is sound because creating a
        # task does not run it: nothing between that creation and this emit
        # suspends, so ``start`` is still the first thing an observer of this
        # execution sees. Owning every task before the first step that can fail
        # is what lets the reclaim path drain a whole run rather than guess at
        # how much of one exists.
        execution.observation.emit("start", _EventDetails(pid=pid))
        telemetry.emit(LineStreamPhase.SPAWNED)
    except BaseException:
        await _abandon_unstarted_run(run, execution)
        raise
    return run


async def _abandon_unstarted_run(
    run: _LineStreamRun,
    execution: _SubprocessExecution,
) -> None:
    """Terminate, reap, and drain a run that failed before it was returned.

    Reached when post-spawn setup raised, so nothing holds the run: the caller
    never received it, and none of the exits that settle a handed-back run will
    ever see it. The heartbeat stops first, matching the pipeline's spawn
    helper. The child is then stopped and reaped before its streams are
    drained, because both drain policies wait for the consumers to reach EOF
    and a live child can hold its own pipe open. Discarding whatever the drain
    finds is what keeps the post-spawn failure the one that propagates.
    """
    # Imported here, not at module scope, to avoid a cycle: ``_discard_drain``
    # lives in ``cuprum._line_stream.coordinator`` because a test patches its
    # module-level ``_drain_stream_consumers`` name, and that module imports
    # this one for the rest of the spawn machinery.
    from cuprum._line_stream.coordinator import _discard_drain

    run.settlement_started.set()
    await _stop_idle_monitor(execution.idle)
    run.telemetry.emit(LineStreamPhase.TEARDOWN_STARTED)
    await _terminate_all_shielded(
        (run.process,),
        _TeardownPolicy(
            execution.ctx.cancel_grace,
            owns_group=execution.owns_process_group,
        ),
        observations=(execution.observation,),
    )
    await _discard_drain(run, run.process.pid, execution)
    run.telemetry.emit(LineStreamPhase.TEARDOWN_COMPLETED)


def _with_line_sink_hooks(
    execution: _SubprocessExecution,
    setup: _LineStreamSetup,
) -> _SubprocessExecution:
    """Chain the caller's ``on_line`` ahead of the driver's queue sink.

    Each hook is wrapped so its failure is correlated before it escapes. The
    result is rebuilt rather than mutated: the bundle is frozen and the stream
    consumers read ``on_line`` off it.

    Returns
    -------
    _SubprocessExecution
        The execution whose per-line callback feeds both the caller and the
        queue.
    """
    queue_hook = _observed_line_hook(
        _queue_line_sink(
            setup.queue,
            setup.telemetry,
            settlement_started=setup.settlement_started,
        ),
        "queue",
        setup.telemetry,
    )
    chained_hook = (
        queue_hook
        if execution.on_line is None
        else _chain_line_hooks(
            _observed_line_hook(execution.on_line, "callback", setup.telemetry),
            queue_hook,
        )
    )
    return dc.replace(
        execution,
        on_line=_stop_line_hook_after_settlement(
            chained_hook,
            setup.settlement_started,
        ),
    )


def _stop_line_hook_after_settlement(
    hook: _LineHookFn,
    settlement_started: asyncio.Event,
) -> _LineHookFn:
    """Stop decoded line side effects after teardown takes ownership."""

    def deliver(event: LineEvent) -> cabc.Awaitable[None] | None:
        """Skip unpublished lines once the run begins settling."""
        if settlement_started.is_set():
            return None
        return hook(event)

    return deliver


def _build_unstarted_run(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    setup: _LineStreamSetup,
) -> _LineStreamRun:
    """Build the run and own every task it will need, before anything can fail.

    Splitting this from the spawn keeps ownership complete by construction:
    by the time this returns, the stdin writer and both consumers exist, so
    the caller's first fallible step — the ``start`` emission — already has a
    whole run to abandon rather than a partial one to guess at. Nothing here
    suspends or fails, so no exception can escape and leave a child running
    with half-built ownership.

    Returns
    -------
    _LineStreamRun
        The unstarted run, owning the process and every task that reads it.
    """
    # The same wiring ``run()`` uses, so iterating lines can never silently
    # diverge from it on capture, echo, or sink selection.
    spawn = _prepare_stream_spawn(
        execution,
        process.pid,
        discard_on_cancel=setup.discard_on_cancel,
        settlement_started=setup.settlement_started,
    )
    return _LineStreamRun(
        process=process,
        tasks=_build_run_task_ownership(process, execution, spawn),
        queue=setup.queue,
        settlement_started=setup.settlement_started,
        execution=execution,
        telemetry=setup.telemetry,
    )
