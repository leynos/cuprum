"""Bounded retention of asynchronous observe-hook tasks.

An observed execution schedules one task per awaitable observe hook per event
and keeps them in a single ``pending_tasks`` list until finalization drains it.
Because a line event is emitted per decoded line even when capture and echo are
off, an unbounded list lets a chatty child exhaust parent memory. This module
owns the ceiling, the pruning rule that keeps the list honest, and the admission
command line observation uses to apply backpressure.

The list is shared by every stage of a pipeline, so the ceiling is per run
rather than per stream. Nothing here touches the event dispatcher itself; the
dispatch that produces the tasks lives in :mod:`cuprum._observability`, and the
recorder the helpers log through lives in :mod:`cuprum._retention_telemetry`.
"""

from __future__ import annotations

import asyncio

from cuprum._retention_telemetry import (
    _WAIT_CANCELLED,
    _WAIT_INTERRUPTED,
    _WAIT_RESUMED,
    _RetentionTelemetry,
)

# How many unfinished async observe-hook tasks one execution may retain before
# line observation applies backpressure. The value matches
# ``cuprum._line_stream._LINE_QUEUE_CAPACITY``: the two bounds describe the same
# quantity — how much asynchronous work one observed run may have in flight — so
# a slow observer cannot queue more work than the line driver already queues,
# and a chatty child is paused once either bound is reached. The retention list
# is shared by every stage of a pipeline, so the ceiling is per run rather than
# per stream.
_OBSERVE_TASK_CEILING = 256


def _prune_finished_hook_tasks(
    pending_tasks: list[asyncio.Task[None]],
    *,
    telemetry: _RetentionTelemetry | None = None,
    phase: str | None = None,
    ceiling: int = _OBSERVE_TASK_CEILING,
) -> int:
    """Drop settled hook tasks while preserving the failure that will surface.

    ``_wait_for_exec_hook_tasks`` reports the *first* failed or cancelled task
    in list order; every later failure is discarded unread. Once an execution
    has run long enough, retaining successful tasks (which can never fail) and
    later failures (which can never be reported) buys nothing but memory. This
    helper keeps only what finalization can still use: every unfinished task,
    plus the earliest settled failure, exactly the task the gather would have
    raised.

    The list is mutated in place because it is shared by every stage of a
    pipeline; rebinding it would leave other stages retaining into a list that
    finalization never drains.

    Exceptions are retrieved from every dropped failed task before it is
    removed. A task destroyed with an unretrieved exception logs "Task
    exception was never retrieved" at garbage-collection time, which would
    blame the drop for a failure that was never going to be reported.

    This is also the only place the depth is measured. Counting the tasks kept
    as they go costs nothing here, whereas doing it on every line would add a
    full scan of the shared list to the hot path; the depth telemetry and the
    admission decision both reuse the count recorded here.

    Parameters
    ----------
    pending_tasks:
        The execution's shared retention list, pruned in place.
    telemetry:
        Optional recorder for the prune count and its structured log.
    phase:
        Closed event-phase label for the log record.
    ceiling:
        The admission ceiling, recorded for correlation only.

    Returns
    -------
    int
        The number of tasks dropped.

    """
    kept: list[asyncio.Task[None]] = []
    unfinished = 0
    retained_failure = False
    pruned_failures = 0
    for task in pending_tasks:
        if not task.done():
            kept.append(task)
            unfinished += 1
            continue
        # ``cancelled()`` must be checked first: calling ``exception()`` on a
        # cancelled task raises the ``CancelledError`` it was cancelled with.
        failed = task.cancelled() or task.exception() is not None
        if failed and not retained_failure:
            retained_failure = True
            kept.append(task)
            continue
        if failed:
            pruned_failures += 1
    pruned = len(pending_tasks) - len(kept)
    pending_tasks[:] = kept
    if telemetry is not None:
        telemetry.record_depth(unfinished, phase=phase)
        telemetry.record_prune(
            pruned=pruned,
            pruned_failures=pruned_failures,
            ceiling=ceiling,
            phase=phase,
        )
    return pruned


def _prune_for_capacity(
    pending_tasks: list[asyncio.Task[None]],
    ceiling: int,
    *,
    telemetry: _RetentionTelemetry | None = None,
    phase: str | None = None,
) -> bool:
    """Prune at the ceiling and report whether one more hook task may start.

    This is a command, not a query. Reaching the ceiling prunes settled tasks
    in place and retrieves their exceptions before the slot count is judged, so
    the mutation and the depth and prune samples it publishes are part of the
    decision rather than hidden side effects of a read. The name says so: a
    caller may not read it as a ``has_*`` capacity query. The fast path below
    the ceiling only compares the raw list length: the extra scan would be pure
    overhead, and the list only ever holds settled tasks long enough to cross
    that threshold.

    It records no admission. The capacity-wait loop re-checks after every
    completion, and those re-checks belong to one deferred line rather than to
    new admissions, so only the caller that decides to schedule a task records
    one.

    Parameters
    ----------
    pending_tasks:
        The execution's shared retention list; pruned in place when the raw
        length has reached ``ceiling``.
    ceiling:
        Maximum number of unfinished hook tasks to retain; must be positive.
    telemetry:
        Optional recorder for the depth the prune computes.
    phase:
        Closed event-phase label for the log record.

    Returns
    -------
    bool
        ``True`` when the number of unfinished tasks is below ``ceiling``.

    """
    if len(pending_tasks) < ceiling:
        return True
    _prune_finished_hook_tasks(
        pending_tasks,
        telemetry=telemetry,
        phase=phase,
        ceiling=ceiling,
    )
    if telemetry is not None:
        # The prune counted the unfinished tasks it kept while walking the
        # list, exactly the quantity this query needs.
        return telemetry.depth < ceiling
    return sum(1 for task in pending_tasks if not task.done()) < ceiling


def _admit_hook_capacity(
    pending_tasks: list[asyncio.Task[None]],
    ceiling: int,
    *,
    telemetry: _RetentionTelemetry | None = None,
    phase: str | None = None,
) -> bool:
    """Decide one admission and record it.

    Parameters
    ----------
    pending_tasks:
        The execution's shared retention list; pruned in place by the decision.
    ceiling:
        Maximum number of unfinished hook tasks to retain; must be positive.
    telemetry:
        Optional recorder for the decision and the resulting depth.
    phase:
        Closed event-phase label for the log record.

    Returns
    -------
    bool
        ``True`` when a task may be scheduled.

    """
    admitted = _prune_for_capacity(
        pending_tasks,
        ceiling,
        telemetry=telemetry,
        phase=phase,
    )
    if telemetry is not None:
        telemetry.record_admission(
            admitted=admitted,
            ceiling=ceiling,
            phase=phase,
            retained=len(pending_tasks),
        )
    return admitted


def _begin_capacity_wait(
    telemetry: _RetentionTelemetry | None,
    *,
    ceiling: int,
    phase: str | None,
) -> float:
    """Log the saturation and return the instant the wait is measured from.

    Returns
    -------
    float
        The clock reading to subtract when the wait ends, or ``0.0`` when no
        recorder is installed.
    """
    if telemetry is None:
        return 0.0
    started = telemetry.clock()
    telemetry.log_capacity_wait(ceiling=ceiling, phase=phase)
    return started


async def _wait_for_hook_capacity(
    pending_tasks: list[asyncio.Task[None]],
    ceiling: int,
    *,
    telemetry: _RetentionTelemetry | None = None,
    phase: str | None = None,
) -> str:
    """Block until a slot frees, returning the ``resumed`` outcome.

    The refused admission that precedes this call guarantees at least
    ``ceiling`` unfinished tasks, so the wait set is never empty. Each re-check
    prunes and observes depth but records no admission: the completions that
    free capacity belong to the one line already waiting, and counting them
    would inflate the admission total.

    Returns
    -------
    str
        The ``resumed`` wait outcome, once a slot has freed.
    """
    while True:
        unfinished = [task for task in pending_tasks if not task.done()]
        _ = await asyncio.wait(unfinished, return_when=asyncio.FIRST_COMPLETED)
        if _prune_for_capacity(
            pending_tasks,
            ceiling,
            telemetry=telemetry,
            phase=phase,
        ):
            return _WAIT_RESUMED


async def _await_hook_capacity(
    pending_tasks: list[asyncio.Task[None]],
    ceiling: int,
    *,
    telemetry: _RetentionTelemetry | None = None,
    phase: str | None = None,
) -> None:
    """Wait until a hook task may be scheduled, then return.

    This is the backpressure primitive for line observation. Waiting on
    :func:`asyncio.wait` with ``FIRST_COMPLETED`` rather than gathering the
    pending tasks matters for two reasons: gathering would also await the
    remaining tasks (holding the caller far longer than a slot needs), and
    cancellation of this waiter releases the wait without touching the
    observer tasks themselves. Those tasks belong to the execution and are
    settled by its final drain, so a cancelled line driver must not be able to
    cancel them out from under it.

    The wait itself is recorded on every exit path, including cancellation, so
    a suspended-then-abandoned line is not invisible to an operator. A failing
    retention sink propagates from that record on every path except
    cancellation: there it is logged instead, because a sink error must not
    replace ``CancelledError`` and break the caller's cancellation contract.

    Parameters
    ----------
    pending_tasks:
        The execution's shared retention list; pruned in place.
    ceiling:
        Maximum number of unfinished hook tasks to retain; must be positive.
    telemetry:
        Optional recorder for the saturation, wait transition, and duration.
    phase:
        Closed event-phase label for the log records.

    Raises
    ------
    asyncio.CancelledError
        If the waiter is cancelled while suspended; the wait is recorded with
        the ``cancelled`` outcome before the exception propagates. A retention
        sink that fails while recording it is logged rather than raised, so
        cancellation still reaches the caller.

    """
    if _admit_hook_capacity(
        pending_tasks,
        ceiling,
        telemetry=telemetry,
        phase=phase,
    ):
        return
    started = _begin_capacity_wait(telemetry, ceiling=ceiling, phase=phase)
    outcome = _WAIT_INTERRUPTED
    try:
        outcome = await _wait_for_hook_capacity(
            pending_tasks,
            ceiling,
            telemetry=telemetry,
            phase=phase,
        )
    except asyncio.CancelledError:
        outcome = _WAIT_CANCELLED
        raise
    finally:
        if telemetry is not None:
            telemetry.record_wait_outcome(
                started=started,
                ceiling=ceiling,
                phase=phase,
                outcome=outcome,
            )


__all__ = [
    "_OBSERVE_TASK_CEILING",
    "_admit_hook_capacity",
    "_await_hook_capacity",
    "_prune_finished_hook_tasks",
    "_prune_for_capacity",
]
