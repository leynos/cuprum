"""Bounded retention of asynchronous observe-hook tasks.

An observed execution schedules one task per awaitable observe hook per event
and keeps them in a single ``pending_tasks`` list until finalization drains it.
Because a line event is emitted per decoded line even when capture and echo are
off, an unbounded list lets a chatty child exhaust parent memory. This module
owns the ceiling, the pruning rule that keeps the list honest, and the admission
pair line observation uses to apply backpressure.

The list is shared by every stage of a pipeline, so the ceiling is per run
rather than per stream. Nothing here touches the event dispatcher itself; the
dispatch that produces the tasks lives in :mod:`cuprum._observability`.
"""

from __future__ import annotations

import asyncio

# How many unfinished async observe-hook tasks one execution may retain before
# line observation applies backpressure. The value matches
# ``cuprum._line_stream._LINE_QUEUE_CAPACITY``: the two bounds describe the same
# quantity — how much asynchronous work one observed run may have in flight — so
# a slow observer cannot queue more work than the line driver already queues,
# and a chatty child is paused once either bound is reached. The retention list
# is shared by every stage of a pipeline, so the ceiling is per run rather than
# per stream.
_OBSERVE_TASK_CEILING = 256


def _prune_finished_hook_tasks(pending_tasks: list[asyncio.Task[None]]) -> None:
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

    Parameters
    ----------
    pending_tasks:
        The execution's shared retention list, pruned in place.

    """
    kept: list[asyncio.Task[None]] = []
    retained_failure = False
    for task in pending_tasks:
        if not task.done():
            kept.append(task)
            continue
        # ``cancelled()`` must be checked first: calling ``exception()`` on a
        # cancelled task raises the ``CancelledError`` it was cancelled with.
        failed = task.cancelled() or task.exception() is not None
        if failed and not retained_failure:
            retained_failure = True
            kept.append(task)
    pending_tasks[:] = kept


def _has_hook_capacity(
    pending_tasks: list[asyncio.Task[None]],
    ceiling: int,
) -> bool:
    """Return whether one more hook task may be scheduled right now.

    The fast path counts the whole list rather than scanning for unfinished
    tasks: below the ceiling the extra work would be pure overhead, and the
    list only ever holds settled tasks long enough to cross that threshold.

    Parameters
    ----------
    pending_tasks:
        The execution's shared retention list; pruned in place when the raw
        length has reached ``ceiling``.
    ceiling:
        Maximum number of unfinished hook tasks to retain; must be positive.

    Returns
    -------
    bool
        ``True`` when the number of unfinished tasks is below ``ceiling``.

    """
    if len(pending_tasks) < ceiling:
        return True
    _prune_finished_hook_tasks(pending_tasks)
    return sum(1 for task in pending_tasks if not task.done()) < ceiling


async def _await_hook_capacity(
    pending_tasks: list[asyncio.Task[None]],
    ceiling: int,
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

    Parameters
    ----------
    pending_tasks:
        The execution's shared retention list; pruned in place.
    ceiling:
        Maximum number of unfinished hook tasks to retain; must be positive.

    """
    while not _has_hook_capacity(pending_tasks, ceiling):
        # ``_has_hook_capacity`` returned ``False``, so at least ``ceiling``
        # (and therefore at least one) unfinished task remains: the list
        # passed to ``asyncio.wait`` is never empty.
        unfinished = [task for task in pending_tasks if not task.done()]
        _ = await asyncio.wait(unfinished, return_when=asyncio.FIRST_COMPLETED)


__all__ = [
    "_OBSERVE_TASK_CEILING",
    "_await_hook_capacity",
    "_has_hook_capacity",
    "_prune_finished_hook_tasks",
]
