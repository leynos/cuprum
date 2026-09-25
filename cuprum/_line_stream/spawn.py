"""Build and, on failure, unwind one unstarted ``SafeCmd.lines()`` run.

Part of the ``cuprum._line_stream`` package. This module owns the pieces that
exist before the run is handed back to its caller: the stdin writer, the stream
consumers, the chained line hooks that feed both the caller's callback and the
queue, and the teardown of a run that failed before it could be returned.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._line_callbacks import _chain_line_hooks
from cuprum._line_stream.line_queue import (
    _LineStreamRun,
    _observed_line_hook,
    _queue_line_sink,
)
from cuprum._process_lifecycle import _terminate_all_shielded
from cuprum._streams import _RelayDiagnostics
from cuprum._subprocess_stdin import _spawn_stdin_writer
from cuprum._subprocess_streams import (
    _build_stream_config,
    _spawn_stream_consumers,
    _StreamConsumerSpawnContext,
)
from cuprum._subprocess_wait import _RunTaskOwnership
from cuprum.line_stream_events import LineStreamPhase

if typ.TYPE_CHECKING:
    from cuprum._line_stream.line_queue import _LineQueueItem
    from cuprum._line_stream.telemetry import _LineStreamTelemetry
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.lines import _LineHookFn

__all__ = ["_abandon_unstarted_run", "_build_unstarted_run", "_with_line_sink_hooks"]


async def _abandon_unstarted_run(
    run: _LineStreamRun,
    execution: _SubprocessExecution,
) -> None:
    """Terminate, reap, and drain a run that failed before it was returned.

    Reached when post-spawn setup raised, so nothing holds the run: the caller
    never received it, and none of the exits that settle a handed-back run will
    ever see it. The heartbeat stops first — the pipeline's spawn helper does
    the same, and a keepalive narrating a child that is already being torn down
    is worse than no keepalive at all. The child is then stopped and reaped
    before its streams are drained, because both drain policies wait for the
    consumers to reach EOF and a live child can hold its own pipe open.
    Discarding whatever the drain finds is what keeps the post-spawn failure the
    one that propagates.
    """
    # Imported here, not at module scope, to avoid a cycle: ``_discard_drain``
    # lives in ``cuprum._line_stream.coordinator`` because a test patches its
    # module-level ``_drain_stream_consumers`` name, and that module imports
    # this one for the rest of the spawn machinery.
    from cuprum._line_stream.coordinator import _discard_drain

    await _stop_idle_monitor(execution.idle)
    run.telemetry.emit(LineStreamPhase.TEARDOWN_STARTED)
    await _terminate_all_shielded((run.process,), execution.ctx.cancel_grace)
    await _discard_drain(run, run.process.pid, execution)
    run.telemetry.emit(LineStreamPhase.TEARDOWN_COMPLETED)


def _with_line_sink_hooks(
    execution: _SubprocessExecution,
    queue: asyncio.Queue[_LineQueueItem],
    telemetry: _LineStreamTelemetry,
) -> _SubprocessExecution:
    """Chain the caller's ``on_line`` ahead of the driver's queue sink.

    Each hook is wrapped so its failure is correlated before it escapes, and
    the result is a rebuilt execution rather than a mutated one: the bundle is
    a frozen dataclass, and the stream consumers read ``on_line`` off it.

    Returns
    -------
    _SubprocessExecution
        The execution whose per-line callback feeds both the caller and the
        queue.
    """
    hooks: list[_LineHookFn] = []
    if execution.on_line is not None:
        hooks.append(_observed_line_hook(execution.on_line, "callback", telemetry))
    hooks.append(
        _observed_line_hook(_queue_line_sink(queue, telemetry), "queue", telemetry)
    )
    return dc.replace(execution, on_line=_chain_line_hooks(hooks))


def _build_unstarted_run(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    queue: asyncio.Queue[_LineQueueItem],
    telemetry: _LineStreamTelemetry,
) -> _LineStreamRun:
    """Build the run and own every task it will need, before anything can fail.

    Splitting this from the spawn keeps the ownership complete by construction:
    by the time this returns, the stdin writer and both consumers exist, so the
    caller's first fallible step — the ``start`` emission — already has a whole
    run to abandon rather than a partial one to guess at.

    Nothing here suspends or fails. ``_build_stream_config`` only reads the
    execution, ``_spawn_stdin_writer`` and ``_spawn_stream_consumers`` are plain
    ``create_task`` calls, and ``_LineStreamRun`` is a frozen dataclass, so no
    exception can escape and leave a child running with half-built ownership.

    Returns
    -------
    _LineStreamRun
        The unstarted run, owning the process and every task that reads it.
    """
    discard_on_cancel = asyncio.Event()
    stream_config = _build_stream_config(execution, discard_on_cancel)
    relay_diagnostics = (_RelayDiagnostics(), _RelayDiagnostics())
    # The same consumer builder ``run()`` uses, so iterating lines can never
    # silently diverge from it on capture, echo, or sink selection.
    spawn_context = _StreamConsumerSpawnContext(
        stream_config=stream_config,
        pid=process.pid,
        relay_diagnostics=relay_diagnostics,
    )
    return _LineStreamRun(
        process=process,
        tasks=_RunTaskOwnership(
            stdin_task=_spawn_stdin_writer(
                process, execution.stdin_data, execution.observation
            ),
            consumers=_spawn_stream_consumers(
                process,
                execution,
                spawn_context,
            ),
            discard_on_cancel=discard_on_cancel,
            relay_diagnostics=relay_diagnostics,
            idle=execution.idle,
        ),
        queue=queue,
        started_at=execution.started_at,
        telemetry=telemetry,
    )
