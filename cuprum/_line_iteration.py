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
import dataclasses as dc
import typing as typ

from cuprum._line_stream_finalization import (
    _LineIterationState,
    _settle_line_iteration,
)
from cuprum._pipeline_types import _EventDetails
from cuprum._sink_lifecycle import _outcome_for_result
from cuprum.lines import LineEvent

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import types

    from cuprum._execution_tracking import _ExecutionTracking
    from cuprum._line_stream import _LineQueueItem
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.sh import CommandResult
    from cuprum.sh.line_options import _PreparedLineIteration


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
        # coordinator's waiters deterministically, not at garbage collection.
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
    execution_factory: cabc.Callable[[], _SubprocessExecution],
    tracking: _ExecutionTracking,
    *,
    requested_timeout: float | None,
    open_sink: cabc.Callable[[], None],
) -> cabc.AsyncGenerator[LineEvent | CommandResult, None]:
    """Yield each ``LineEvent``, then the run's ``CommandResult``.

    The driver coroutine owns the subprocess; this generator only drains the
    queue it feeds. However it ends — the terminal result, an ``aclose()``,
    or cancellation — the coordinator is reconciled, so the child gets
    ``SIGTERM``, the cancel grace wait, then ``SIGKILL``. A bare ``break``
    ends nothing; ``async with`` or ``aclose()`` guarantees teardown.

    The presentation sink is framed for exactly as long as this generator is
    alive. Opening happens on the first pull, not when the stream is built, so
    a stream that is never iterated frames nothing; closing runs in the same
    shielded finalizer as every other teardown, so an early close or a
    cancellation releases the adapter's lease and its group rather than
    stranding them. The delivery worker is closed first: mirrored lines it
    still holds have to land inside the framing.

    A generator body runs in the task that pulls it, so the authoritative
    checks live here, not in ``SafeCmd.lines()``. ``open_sink`` frames the
    configured presentation sink exactly once and is called before the bundle
    is built, so its log destinations are the session's.

    Yields
    ------
    LineEvent | CommandResult
        One line event per decoded output line, then the run's result.
    """
    # Setup and teardown each live in one helper. The state record carries a
    # partial build to the finalizer, so a failure before the built names are
    # bound here still releases the sink session and the delivery worker.
    state = _LineIterationState()
    failure: BaseException | None = None
    result: CommandResult | None = None
    try:
        started = _start_line_iteration(
            state,
            execution_factory,
            requested_timeout=requested_timeout,
            open_sink=open_sink,
        )
        async for event in _stream_line_events(started):
            yield event
        result = await started.result_future
        _publish_line_completion(started.execution, result)
        yield result
    except BaseException as error:
        # Broader than ``Exception`` on purpose: the teardown classifies what
        # ended iteration, and a ``GeneratorExit`` from a close or a
        # ``CancelledError`` from the caller's own cancellation are not
        # failures to pair with a hook error.
        failure = error
        raise
    finally:
        await _settle_line_iteration(state, tracking, result, failure)


def _open_line_stream(
    prepared: _PreparedLineIteration,
    *,
    requested_timeout: float | None,
) -> LineStream:
    """Build the lazy stream over a prepared line iteration.

    ``requested_timeout`` is the raw timeout, not the value preparation
    resolved: re-resolving that value would keep a timeout from a scope the
    caller has since left, and ``None`` must stay ``None`` so the consuming
    scope's default can apply.

    Returns
    -------
    LineStream
        The lazy stream the caller iterates.
    """
    return LineStream(
        _iter_line_events(
            prepared.execution_factory,
            prepared.tracking,
            requested_timeout=requested_timeout,
            open_sink=prepared.open_sink,
        )
    )


@dc.dataclass(frozen=True, slots=True)
class _StartedLineIteration:
    """A line iteration that has been authorized, planned, and started."""

    execution: _SubprocessExecution

    queue: asyncio.Queue[_LineQueueItem]

    result_future: asyncio.Future[CommandResult]


def _start_line_iteration(
    state: _LineIterationState,
    execution_factory: cabc.Callable[[], _SubprocessExecution],
    *,
    requested_timeout: float | None,
    open_sink: cabc.Callable[[], None],
) -> _StartedLineIteration:
    """Frame the sink, resolve the run, and start the coordinator.

    The opener runs first: the bundle resolves its destinations from the
    session, and a refresh refusal after it still owes the bracket close the
    caller's finalizer performs. Each step records itself on *state* as it
    completes, so closing a half-built run releases exactly what exists.

    The plan event and the before hooks run here, inside the caller's ``try``,
    rather than ahead of it: both emit, and a synchronous observe hook that
    raises on either would otherwise strand the async-hook tasks already
    queued behind it, which only this iteration's reconcile drains.

    Returns
    -------
    _StartedLineIteration
        The started run, holding its queue, result future, and bundle.
    """
    from cuprum._line_stream import (
        _line_event_queue,
        _refresh_line_stream_execution,
    )

    open_sink()
    execution = execution_factory()
    state.execution = execution
    execution = _refresh_line_stream_execution(execution, requested_timeout)
    state.execution = execution
    queue = _line_event_queue()
    result_future = asyncio.get_running_loop().create_future()
    state.result_future = result_future
    execution.observation.emit("plan", _EventDetails(pid=None))
    for hook in execution.observation.hooks.before_hooks:
        hook(execution.cmd)
    state.coordinator = asyncio.create_task(
        _drive_line_stream(execution, queue, result_future)
    )
    return _StartedLineIteration(execution, queue, result_future)


async def _stream_line_events(
    started: _StartedLineIteration,
) -> cabc.AsyncGenerator[LineEvent, None]:
    """Yield the run's line events, stopping once its outcome is published."""
    while True:
        item = await _next_queue_item(started.queue, started.result_future)
        if not isinstance(item, LineEvent):
            return
        yield item


def _publish_line_completion(
    execution: _SubprocessExecution,
    result: CommandResult,
) -> None:
    """Run the refreshed completion hooks and publish the terminal event."""
    for hook in execution.observation.hooks.after_hooks:
        hook(execution.cmd, result)
    outcome = _outcome_for_result(result)
    execution.observation.emit_terminal(
        outcome.outcome,
        _EventDetails(
            pid=result.pid,
            exit_code=result.exit_code,
            duration_s=result.duration,
        ),
    )


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
        # The coordinator reads the run's own bundle: the spawn helper stamped
        # the run's monotonic start onto its copy, and the post-exit deadline is
        # a remainder from that reference, not from the ``0.0`` copy here.
        await _coordinate_line_stream(run, queue, result_future)
    except asyncio.CancelledError:
        raise
    except BaseException as error:  # ruff: ignore[blind-except] - any failure reaches the iterator
        if not result_future.done():
            result_future.set_exception(error)


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
        # ``asyncio.wait`` leaves the loser pending, so a published failure or a
        # cancellation would park the getter on a queue nobody will read again.
        if not getter.done():
            getter.cancel()
            await asyncio.gather(getter, return_exceptions=True)


__all__ = ["LineStream"]
