"""The record a handle owns a child through, and the spawn it starts from.

``_HandleRun`` groups what a running command needs held together: the process,
the execution bundle it was spawned from, the tracking that owns its sink
bracket and observe-hook tasks, and the task ownership that reconciles its
stdin writer and stream consumers.

Every handle owns a :class:`_RunTaskOwnership`, including one whose streams
were wired to ``DEVNULL``. That is a deliberate flattening: a consumer over an
absent stream completes immediately, so keeping the field non-optional means
the handle has one reconciliation path, one wait path, and one teardown path
instead of a capture-dependent fork in all three. The cost is two tasks that
finish before they are ever awaited.

``_build_handle_execution`` strips the standing deadline. ``start()`` admits no
deadline of its own — the handle exists so the caller decides when to stop
waiting, so a timeout could only be settled against a wait that has not
happened yet. A caller who wants one supplies it to
``RunningCommand.wait(timeout=...)``, where the clock it is measured against
actually starts.

The idle monitor is *not* detached here, and that is load-bearing in the other
direction: the monitor's presence is what decides whether the child's streams
are piped at all, so detaching it would change how the child is wired. The
handle arms it when it starts waiting rather than at spawn, because a caller
who holds a handle without waiting has not asked the child to be narrated.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ
from time import perf_counter

from cuprum._pipeline_types import _EventDetails
from cuprum._streams import _RelayDiagnostics
from cuprum._subprocess_spawn import (
    _spawn_subprocess,
    _SubprocessExecution,
)
from cuprum._subprocess_stdin import _spawn_stdin_writer
from cuprum._subprocess_streams import (
    _build_stream_config,
    _spawn_stream_consumers,
    _StreamConsumerSpawnContext,
)
from cuprum._subprocess_wait import _RunTaskOwnership

if typ.TYPE_CHECKING:
    from cuprum._execution_tracking import _ExecutionTracking

__all__ = [
    "_HandleRun",
    "_attach_run_tasks",
    "_build_handle_execution",
    "_launch_idle_monitor",
    "_start_handle_run",
]


@dc.dataclass(frozen=True, slots=True)
class _HandleRun:
    """Everything one owned child needs to be driven and settled.

    ``tracking`` is held even though the handle seldom consults it: the sink
    bracket it carries has to be closed exactly once and the observe-hook
    tasks a plan observer scheduled have to be drained, and both of those
    happen at settlement rather than at spawn.

    ``started_at`` is the monotonic reference the run's durations are measured
    against; ``wall_clock_started_at`` is the separate instant the published
    result reports, so a consumer correlating a result with a log line is not
    asked to convert between clock bases.
    """

    process: asyncio.subprocess.Process
    execution: _SubprocessExecution
    started_at: float
    wall_clock_started_at: float
    tracking: _ExecutionTracking
    ownership: _RunTaskOwnership


def _build_handle_execution(execution: _SubprocessExecution) -> _SubprocessExecution:
    """Return the bundle a handle spawns from, with no standing deadline.

    Returns
    -------
    _SubprocessExecution
        The bundle with its timeout cleared, so only the deadline a caller
        supplies to :meth:`RunningCommand.wait` is ever enforced.
    """
    # Rebuilt rather than mutated: the bundle is a frozen dataclass, and
    # ``wait`` derives its own copy from this one by replacing the timeout
    # again, so the standing value has to be absent from the base it copies.
    return dc.replace(execution, timeout=None)


async def _start_handle_run(
    execution: _SubprocessExecution,
    tracking: _ExecutionTracking,
) -> _HandleRun:
    """Spawn the child, announce it, and take ownership of its tasks.

    The record is built by rebuilding rather than mutating, because the two
    clock readings have to be taken at their stated points: the monotonic
    reference *before* the spawn, so the child's recorded duration includes the
    time its spawn blocked, and the wall-clock instant *before* the spawn too,
    so a result's ``started_at`` is never later than the child it describes.

    Everything after the spawn is emission-free until the ``start`` event, so
    the child is never running without a stamped record and an announced PID to
    match it. A ``start`` event whose synchronous observe hook raises is the one
    failure that can strand a spawned child, so it is owned here: the
    termination path in ``cuprum._process_handle.control`` reclaims it before
    the failure propagates. That reclaim is why the ``start`` event, and not
    merely the spawn, is inside the guard: a child nobody has been told about
    is still a child nobody would reap.

    Returns
    -------
    _HandleRun
        The owned record, once the child is running and its ``start`` event has
        been emitted.

    Raises
    ------
    BaseException
        Whatever the ``start`` emission raised, once the child has been
        terminated and reaped.
    """  # ruff: ignore[docstring-extraneous-exception] - the guard re-raises whatever the emission raised
    started_at = perf_counter()
    wall_clock_started_at = execution.observation.wall_clock()
    process = await _spawn_subprocess(execution)
    try:
        execution.observation.emit("start", _EventDetails(pid=process.pid))
    except BaseException:
        # Imported here so this module and the control module do not import
        # each other at module scope.
        from cuprum._process_handle.control import _terminate_shielded

        await _terminate_shielded(process, execution.ctx.cancel_grace)
        raise
    return _HandleRun(
        process=process,
        execution=execution,
        started_at=started_at,
        wall_clock_started_at=wall_clock_started_at,
        tracking=tracking,
        ownership=_attach_run_tasks(process, execution),
    )


def _launch_idle_monitor(execution: _SubprocessExecution) -> None:
    """Arm the run's watchdog when it has one; idempotent.

    Called once the handle is settling or being stopped, never at spawn, so
    the time a caller spends holding a handle without waiting is not counted
    as the child's silence.
    """
    if execution.idle is not None:
        execution.idle.launch()


def _attach_run_tasks(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
) -> _RunTaskOwnership:
    """Start the stdin writer and the stream consumers for an owned child.

    The tasks are wired exactly as the streamed ``run()`` path wires them —
    same config, same spawn context, same stdin writer — because the handle is
    a second way to own a ``run()``, not a second implementation of one.
    Creating a task does not run it, so the whole set is owned before the
    first step that can fail.

    Returns
    -------
    _RunTaskOwnership
        The writer and consumers, with the discard latch and diagnostics
        collectors their single reconciliation point needs.
    """
    discard_on_cancel = asyncio.Event()
    stream_config = _build_stream_config(execution, discard_on_cancel)
    relay_diagnostics = (_RelayDiagnostics(), _RelayDiagnostics())
    spawn_context = _StreamConsumerSpawnContext(
        stream_config=stream_config,
        pid=process.pid,
        relay_diagnostics=relay_diagnostics,
    )
    return _RunTaskOwnership(
        stdin_task=_spawn_stdin_writer(
            process,
            execution.stdin_data,
            execution.observation,
        ),
        consumers=_spawn_stream_consumers(process, execution, spawn_context),
        discard_on_cancel=discard_on_cancel,
        relay_diagnostics=relay_diagnostics,
        idle=execution.idle,
    )
