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

from cuprum._pipeline_types import _EventDetails
from cuprum._subprocess_spawn import (
    _spawn_subprocess,
    _SubprocessExecution,
)
from cuprum._subprocess_stdin import _spawn_stdin_writer
from cuprum._subprocess_stdin_stream import _stdin_codec
from cuprum._subprocess_streams import (
    _build_task_ownership,
    _prepare_stream_spawn,
    _spawn_stream_consumers,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._execution_tracking import _ExecutionTracking
    from cuprum._subprocess_wait_types import _RunTaskOwnership

__all__ = [
    "_HandleRun",
    "_attach_run_tasks",
    "_build_handle_execution",
    "_detach_run_tasks",
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

    ``stop_lock`` serializes the stops, which the settlement does not: that
    shares the *outcome*, while this shares the *escalation* that produces it.
    It lives on the child's own record because every route that escalates must
    take the same lock for any of them to be excluded — a handle's
    ``terminate``, a teardown, and the settlement's own ``_expire`` are three
    routes to one child, so a lock each of them held a different copy of would
    exclude nothing. Two concurrent escalations of one child are what must not
    happen: a child with its own ``SIGTERM`` handler that is slow to die is
    escalated to ``SIGKILL`` by the first stop, and a second stop racing that
    grace period would deliver the forced signals a second time.

    It is off by default so a hand-built record — a test double that holds no
    child — needs no event loop at construction.
    """

    process: asyncio.subprocess.Process
    execution: _SubprocessExecution
    started_at: float
    wall_clock_started_at: float
    tracking: _ExecutionTracking
    ownership: _RunTaskOwnership
    default_timeout: float | None = None
    stop_lock: asyncio.Lock = dc.field(default_factory=asyncio.Lock)


def _build_handle_execution(execution: _SubprocessExecution) -> _SubprocessExecution:
    """Return the bundle a handle spawns from, with no standing deadline.

    Returns
    -------
    _SubprocessExecution
        The bundle with its timeout cleared, so only the deadline a caller
        supplies to :meth:`RunningCommand.wait` is ever enforced.
    """
    # Rebuilt rather than mutated, because the bundle is a frozen dataclass.
    # The cleared value is not lost: it travels on the record as
    # ``default_timeout``, and the deadline a caller supplies to ``wait`` is
    # applied by the settlement around that caller's own await of the shared
    # task — no bundle carrying a timeout is ever spawned from.
    return dc.replace(execution, timeout=None)


async def _start_handle_run(
    execution: _SubprocessExecution,
    tracking: _ExecutionTracking,
    default_timeout: float | None = None,
) -> _HandleRun:
    """Spawn the child, announce it, and take ownership of its tasks.

    The record is built by rebuilding rather than mutating, because the
    readings it carries have to be taken at their stated points. Both clocks
    are sampled *before* the spawn — the monotonic reference so the child's
    recorded duration includes the time its spawn blocked, the wall-clock
    instant so a result's ``started_at`` is never later than the child it
    describes — and the monotonic reading is stamped onto the bundle the child
    is spawned with, because the stream callbacks read the start instant from
    there rather than from the record this returns.

    Everything after the spawn is emission-free until the ``start`` event, so
    the child is never running without a stamped record and an announced PID to
    match it. The two steps that can fail once the child exists — emitting that
    event, and attaching the tasks that drive its streams — are both inside the
    guard, and the termination path in ``cuprum._process_handle.control``
    reclaims the child before either failure propagates. A child nobody has
    been told about is still a child nobody would reap, and a child whose
    streams nobody attached is in the same position: this function is the only
    place that holds both facts at once, so this is where they are owned.

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
        Whatever the ``start`` emission or the task attachment raised, once the
        child has been terminated and reaped.
    """  # ruff: ignore[docstring-extraneous-exception] - the guard re-raises whatever the emission or the attachment raised
    # Read from the observation rather than this module's clock: the bundle's
    # line callbacks subtract this reference from that same callable when
    # stamping each ``LineEvent``, so a reference from a different clock would
    # turn every stamp into a difference between two clocks under injection.
    started_at = execution.observation.monotonic_clock()
    wall_clock_started_at = execution.observation.wall_clock()
    # Set on the bundle before the spawn, because the record this returns is
    # what the spawn was driven with and the line callbacks built from the
    # bundle read ``started_at`` to timestamp each line: left at its zero
    # default, every ``LineEvent.at`` would report the machine's monotonic
    # uptime instead of seconds since the command started.
    execution = dc.replace(execution, started_at=started_at)
    process = await _spawn_subprocess(execution)
    try:
        execution.observation.emit("start", _EventDetails(pid=process.pid))
        ownership = await _attach_run_tasks(process, execution)
    except BaseException:
        # Imported here so this module and the control module do not import
        # each other at module scope.
        from cuprum._process_handle.control import _terminate_shielded

        # Attaching is inside the guard for the same reason the emission is:
        # a child nobody is driving — no writer started, or consumers nobody
        # has been told about — is still a child nobody would reap. Attaching
        # is transactional, so a partial set of tasks is never what reaches
        # here; whatever it managed to start before failing it has already
        # reclaimed.
        await _terminate_shielded(
            process,
            execution.ctx.cancel_grace,
            owns_group=execution.owns_process_group,
            observation=execution.observation,
        )
        raise
    return _HandleRun(
        process=process,
        execution=execution,
        started_at=started_at,
        wall_clock_started_at=wall_clock_started_at,
        tracking=tracking,
        ownership=ownership,
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


async def _attach_run_tasks(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
) -> _RunTaskOwnership:
    """Start the stdin writer and the stream consumers for an owned child.

    The tasks are wired exactly as the streamed ``run()`` path wires them —
    same config, same spawn context, same stdin writer — because the handle is
    a second way to own a ``run()``, not a second implementation of one.

    Attaching is all-or-nothing. The configuration is built before anything is
    started, because it can fail and a task started ahead of that failure would
    have nobody holding it. Anything started once the tasks begin is reclaimed
    by :func:`_detach_run_tasks` before the failure propagates, so a caller
    that is told attaching failed is not also handed a writer still draining
    into a pipe.

    Returns
    -------
    _RunTaskOwnership
        The writer and consumers, with the discard latch and diagnostics
        collectors their single reconciliation point needs.

    Raises
    ------
    BaseException
        Whatever building or starting the tasks raised, once any task already
        started has been reclaimed.
    """  # ruff: ignore[docstring-extraneous-exception] - the guard re-raises whatever building the tasks raised
    # Built first: this is the step that can fail without having started
    # anything, so failure here needs no cleanup. The preparation helper
    # resolves both stream configs, which is what keeps each stream's capture
    # accumulator reachable from reconciliation.
    spawn = _prepare_stream_spawn(execution, process.pid)
    started: list[asyncio.Task[typ.Any]] = []
    try:
        stdin_task = _spawn_stdin_writer(
            process,
            execution.stdio.stdin,
            _stdin_codec(execution.ctx),
            execution.observation,
        )
        if stdin_task is not None:
            started.append(stdin_task)
        consumers = _spawn_stream_consumers(process, execution, spawn.spawn_context)
        # A redirected stream gets no consumer and holds ``None`` in its slot,
        # so only the tasks actually started are handed to the reclaim path.
        started.extend(consumer for consumer in consumers if consumer is not None)
    except BaseException:
        await _detach_run_tasks(started)
        raise
    return _build_task_ownership(
        stdin_task=stdin_task,
        consumers=consumers,
        spawn=spawn,
        execution=execution,
    )


async def _detach_run_tasks(tasks: cabc.Sequence[asyncio.Task[typ.Any]]) -> None:
    """Cancel and drain the tasks attaching had already started.

    Cancellation is the right reclaim here, unlike the reconciliation a
    settled run performs: nothing has read the child's streams yet, the run
    has no outcome to capture, and the child itself is stopped by the caller
    that owns it. The tasks are gathered with exceptions tolerated so one
    cancelled reader cannot stop the others from being reclaimed.
    """
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
