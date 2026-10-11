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

``_build_handle_execution`` strips the deadline from the bundle a handle
spawns from, because no deadline is enforced while the handle is merely held:
the clock a timeout is measured against starts at the wait that asks for one,
and a bundle carrying a deadline would be one the spawn path could act on.
The resolved value is not discarded, though — it travels on the record as
``default_timeout``, so ``RunningCommand.wait(timeout=...)`` can fall back to
the deadline ``start()`` resolved when its caller supplies none.

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

    ``default_timeout`` is the deadline ``start()`` resolved from its own
    argument, the execution context, and the scoped default, held here because
    the bundle cannot hold it without the spawn path acting on it. Every
    ``wait()`` that supplies no deadline of its own uses it.
    """

    process: asyncio.subprocess.Process
    execution: _SubprocessExecution
    started_at: float
    wall_clock_started_at: float
    tracking: _ExecutionTracking
    ownership: _RunTaskOwnership
    default_timeout: float | None = None


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
    default_timeout: float | None = None,
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

    Parameters
    ----------
    execution : _SubprocessExecution
        The bundle to spawn from, already stripped of any standing deadline.
    tracking : _ExecutionTracking
        The sink bracket and hook tasks the record takes ownership of.
    default_timeout : float | None, default=None
        The deadline every wait on the handle falls back to, already resolved
        from ``start()``'s argument, the context, and the scoped default.

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
        default_timeout=default_timeout,
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
