"""The async iterator ``SafeCmd.lines()`` hands back.

Kept out of ``cuprum.sh`` so the facade stays a facade: the driver coroutine
owns the subprocess through ``cuprum._line_stream``, and this module only turns
the queue that run feeds into something a caller can iterate.

The iterator is a class rather than a bare async generator because the caller
reads the run's ``CommandResult`` after the last line, and because closing the
stream has to be an explicit, awaitable action: ``async for`` does not close a
custom iterator on ``break``.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._observability import (
    _drain_tasks_during_cleanup,
    _wait_for_exec_hook_tasks,
)
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum.lines import LineEvent

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import types

    from cuprum._execution_tracking import _ExecutionTracking
    from cuprum._line_stream import _LineQueueItem
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.sh import CommandResult

_LINES_FINALIZATION_ERROR = "line stream finalization failed"


class LineStream:
    """Async iterator of ``LineEvent`` and the run's final ``CommandResult``.

    Returned by :meth:`SafeCmd.lines`. Iteration yields every decoded output
    line as it arrives; after the run completes, :attr:`result` holds the
    same ``CommandResult`` a ``run()`` call would have returned, including
    captured output when ``capture=True``.

    Leaving the loop early does not, on its own, stop the subprocess. ``async
    for`` never closes a custom iterator on ``break``, and the driver keeps
    draining the child for as long as the stream is reachable. Use the stream
    as an async context manager, or call :meth:`aclose` explicitly, to
    guarantee teardown; dropping the last reference also closes it, but only
    when the event loop finalizes the generator, so that is not a guarantee.

    Either teardown path ends the child the way a cancelled ``run()`` does:
    ``SIGTERM``, the cancel grace wait, then ``SIGKILL``.
    """

    __slots__ = ("_iterator", "result")

    def __init__(
        self,
        iterator: cabc.AsyncGenerator[LineEvent | CommandResult, None],
    ) -> None:
        """Wrap the driver's event generator."""
        self._iterator = iterator
        self.result: CommandResult | None = None

    def __aiter__(self) -> LineStream:
        """Return self as the async iterator."""
        return self

    async def __anext__(self) -> LineEvent:
        """Yield the next line, or set ``result`` and stop at the end."""
        item = await self._iterator.__anext__()
        if isinstance(item, LineEvent):
            return item
        self.result = item
        # The driver generator is finished; closing it here releases the
        # coordinator's waiters deterministically instead of at garbage
        # collection.
        await self._iterator.aclose()
        raise StopAsyncIteration

    async def __aenter__(self) -> LineStream:
        """Return self, so ``async with`` covers the whole iteration."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: types.TracebackType | None,
    ) -> None:
        """Close the stream on every exit from the ``async with`` block."""
        # Never suppresses: teardown failures and the caller's own exception
        # both have to reach them.
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying generator, tearing the run down."""
        await self._iterator.aclose()


async def _iter_line_events(
    execution: _SubprocessExecution,
    tracking: _ExecutionTracking,
) -> cabc.AsyncGenerator[LineEvent | CommandResult, None]:
    """Yield each ``LineEvent``, then the run's ``CommandResult``.

    The driver coroutine owns the subprocess; this generator only drains the
    queue it feeds. Whatever ends the generator — the terminal result, a
    ``break``, a generator close, or task cancellation — the driver task is
    cancelled, which runs the shared reconciliation so the child gets
    ``SIGTERM``, the cancel grace wait, then ``SIGKILL``, and the consumers
    drain exactly once.

    Yields
    ------
    LineEvent | CommandResult
        One ``LineEvent`` per decoded output line, then the run's
        ``CommandResult`` once the subprocess has exited.
    """
    from cuprum._line_stream import _line_event_queue
    from cuprum._pipeline_types import _EventDetails

    queue = _line_event_queue()
    result_future: asyncio.Future[CommandResult] = (
        asyncio.get_running_loop().create_future()
    )
    coordinator: asyncio.Task[None] | None = None
    failure: BaseException | None = None
    # The plan event and the before hooks run inside the try, not ahead of it:
    # both emit, and a synchronous observe hook that raises on either would
    # otherwise strand the async-hook tasks already queued behind it, which only
    # this generator's reconcile drains.
    try:
        execution.observation.emit("plan", _EventDetails(pid=None))
        for hook in tracking.execution_hooks.before_hooks:
            hook(execution.cmd)
        coordinator = asyncio.create_task(
            _drive_line_stream(execution, queue, result_future)
        )
        while True:
            item = await _next_queue_item(queue, result_future)
            if isinstance(item, LineEvent):
                yield item
            else:
                break
        result = await result_future
        _publish_completion(tracking, execution, result)
        yield result
    except BaseException as error:
        # Broader than ``Exception`` on purpose: the teardown classifies what
        # ended iteration, and a ``GeneratorExit`` from a close or a
        # ``CancelledError`` from the caller's own cancellation are not
        # failures to pair with a hook error.
        failure = error
        raise
    finally:
        await _reconcile_line_stream(coordinator, result_future, tracking, failure)


async def _drive_line_stream(
    execution: _SubprocessExecution,
    queue: asyncio.Queue[_LineQueueItem],
    result_future: asyncio.Future[CommandResult],
) -> None:
    """Spawn the run, coordinate it to completion, and publish the outcome.

    Failures are published on the future rather than raised: the coordinator
    task is nobody's awaitable, and the iterator reads the run's outcome off
    that future. A cancellation is the exception — only the iterator cancels
    this task, and only as teardown, so re-raising ends the task cancelled
    rather than handing a ``CancelledError`` to a caller that never cancelled
    anything.

    Raises
    ------
    asyncio.CancelledError
        When the iterator cancels this coordinator while tearing the run down.
    """
    from cuprum._line_stream import (
        _coordinate_line_stream,
        _start_line_stream_run,
    )

    try:
        run = await _start_line_stream_run(execution, queue)
        await _coordinate_line_stream(run, execution, queue, result_future)
    except asyncio.CancelledError:
        raise
    except BaseException as error:  # ruff: ignore[blind-except] - any failure reaches the iterator
        if not result_future.done():
            result_future.set_exception(error)


def _publish_completion(
    tracking: _ExecutionTracking,
    execution: _SubprocessExecution,
    result: CommandResult,
) -> None:
    """Run the caller's after-hooks for a run that completed."""
    for hook in tracking.execution_hooks.after_hooks:
        hook(execution.cmd, result)


async def _reconcile_line_stream(
    coordinator: asyncio.Task[None] | None,
    result_future: asyncio.Future[CommandResult],
    tracking: _ExecutionTracking,
    failure: BaseException | None,
) -> None:
    """Cancel the coordinator when it is still running, then drain its tasks.

    Runs on every exit from iteration — completion, ``break``, generator
    close, and cancellation alike — so the observe-hook tasks are reconciled
    exactly once however iteration ended. ``coordinator`` is ``None`` when a
    failure landed before iteration started one, and the task drain still runs:
    those are the tasks an earlier emit in the same block had already queued.
    """
    if coordinator is not None and not result_future.done():
        coordinator.cancel()
    try:
        if coordinator is not None:
            await _shielded_cleanup(_absorb_coordinator(coordinator, result_future))
    except BaseException as error:
        # A published coordinator failure must not skip observe-hook cleanup.
        # Passing it as the active error means a failing hook is grouped with,
        # rather than replaces, the outcome that ended the run.
        await _shielded_cleanup(_drain_line_stream_tasks(tracking.pending_tasks, error))
        raise
    await _shielded_cleanup(_drain_line_stream_tasks(tracking.pending_tasks, failure))


async def _next_queue_item(
    queue: asyncio.Queue[_LineQueueItem],
    result_future: asyncio.Future[CommandResult],
) -> LineEvent | CommandResult:
    """Get the next queue item, failing fast when the run has already failed.

    The coordinator publishes errors on the future rather than the queue, so
    the queue alone would block forever after a timeout or a spawn failure.
    Racing the two, and preferring the future's outcome, turns a published
    failure into an immediate raise.

    Returns
    -------
    LineEvent | CommandResult
        The next line event, or the terminal result that ends iteration.
    """
    # ``ensure_future`` returns a future unchanged, so ``failure`` *is*
    # ``result_future``: it must outlive this call, because the caller still
    # reads the run's outcome from it. Only the getter is disposable.
    getter = asyncio.ensure_future(queue.get())
    failure = asyncio.ensure_future(result_future)
    try:
        await asyncio.wait(
            {getter, failure},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if failure.done() and not failure.cancelled():
            error = failure.exception()
            if error is not None:
                raise error
        return await getter
    finally:
        # ``asyncio.wait`` leaves the loser pending, so a published failure and
        # a cancellation landing on the wait both leave the getter parked on a
        # queue nobody will read again.
        if not getter.done():
            getter.cancel()
            await asyncio.gather(getter, return_exceptions=True)


async def _absorb_coordinator(
    coordinator: asyncio.Task[None],
    result_future: asyncio.Future[CommandResult],
) -> None:
    """Wait the coordinator out, re-raising a published failure.

    A cancelled coordinator is the ordinary shape of teardown — the iterator
    cancels it once it has stopped consuming — so its cancellation is absorbed
    rather than re-raised, and ``aclose()`` tears the child down without
    surfacing a ``CancelledError`` the caller never issued.
    """
    await asyncio.gather(coordinator, return_exceptions=True)
    if result_future.done() and not result_future.cancelled():
        error = result_future.exception()
        if error is not None:
            raise error


async def _drain_line_stream_tasks(
    pending: list[asyncio.Task[None]],
    failure: BaseException | None,
) -> None:
    """Reconcile observe-hook tasks on every way iteration can end.

    ``lines()`` has more exits than ``run()`` — a caller ``break`` and a
    generator close join completion, timeout, and cancellation — so the drain
    sits in one place instead of on the success path only. Iteration ending on
    a failure aggregates a drain failure with it, so a background hook cannot
    stand in for the error that ended the run. A ``GeneratorExit`` is not a
    failure in that sense, and closing a stream must not raise a group pairing
    it with an unrelated hook error.
    """
    if failure is None or isinstance(failure, GeneratorExit):
        await _wait_for_exec_hook_tasks(pending)
        return
    await _drain_tasks_during_cleanup(
        pending,
        failure,
        message=_LINES_FINALIZATION_ERROR,
    )


__all__ = ["LineStream"]
