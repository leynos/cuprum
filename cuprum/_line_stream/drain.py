"""Drain a line stream's consumers once the child has exited.

Part of the ``cuprum._line_stream`` package. This module owns the single
post-exit collect step, bounded by whatever remains of the run's configured
timeout; the failure-path reconciliation it falls back to lives in
``cuprum._line_stream.coordinator`` because a test patches that module's
``_drain_stream_consumers`` name directly.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._stream_drain import _decode_consumer_drains
from cuprum._subprocess_stdin_reclaim import _settle_stdin_writer
from cuprum._subprocess_timeout import _handle_stream_timeout
from cuprum._subprocess_wait import (
    _consumer_awaitable,
    _deadline_for_collection,
    _drain_context,
    _reconcile_run_tasks,
    _settle_owned_group_before_draining,
)

if typ.TYPE_CHECKING:
    from cuprum._line_stream.line_queue import _LineStreamRun
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_wait_types import (
        _Consumer,
        _DrainContext,
        _DrainPair,
    )

__all__ = ["_collect_line_stream_after_exit"]


async def _collect_line_stream_after_exit(
    run: _LineStreamRun,
    pid: int | None,
    execution: _SubprocessExecution,
) -> _DrainPair:
    """Collect the post-exit drain under the run's remaining configured budget.

    Three things happen once a line stream's child has exited, and each can
    block indefinitely: stopping the idle heartbeat, letting the stdin writer
    finish, and waiting for a consumer to reach end-of-file. All three are
    bounded here by one ``asyncio.timeout`` holding what is left of the run's
    own deadline, measured immediately before the scope it bounds rather than
    once up front — a budget computed earlier would let the await it precedes
    spend part of it and still hand the whole of it to what follows.

    A descendant the child left behind holding an inherited pipe is what makes
    the consumer wait unbounded: no reader ever reaches end-of-file, and the
    exit wait above has already spent the deadline by the time this runs. The
    writer parks for the same reason, on a payload larger than the pipe buffer
    that nothing will drain. Without this bound the run simply never returns,
    and because the hang is what prevents the reconciliation below, the
    descendant outlives the run that spawned it.

    The consumers are observed with ``asyncio.wait`` rather than gathered, and
    that choice is what protects a trailing line. A child that published a final
    line and exited can leave that line sitting in a full queue with its
    consumer parked in ``queue.put``; cancelling a gathered consumer there would
    lose it, whereas ``asyncio.wait`` reports which readers finished and leaves
    the cancellation rule to the reconciliation, which already knows it — cancel
    only what is unfinished, gather with ``return_exceptions=True``, and keep
    every prefix already captured. The wait carries the consumers' own
    remainder, measured at the point of use: the writer await above may already
    have spent part of the budget, so the remainder read there is the one truly
    left. The enclosing scope is the outer backstop for the two awaits that have
    no bound of their own — the heartbeat stop and the writer.

    The writer is settled rather than merely awaited, so a producer parked in
    ``anext`` is reclaimed inside the scope instead of being left for the
    expiry. The settle window is bounded and ends in a cancel; an ``INHERIT``
    run has no group signal to release the writer and depends on it entirely.

    Expiry and cancellation are handled apart, and deliberately. Only the run's
    own budget expiring becomes the translated ``TimeoutExpired`` its caller is
    owed, reconciling with the run's capture policies preserved so the text that
    exception reports is the text the run had already collected. A caller's
    cancellation, a reader's own failure, or any other post-exit error — an
    ``aclose()`` after the child exited, say — discards instead, settling
    promptly rather than spending the capture grace on text nobody will read.

    Returns
    -------
    _DrainPair
        The stdout and stderr drain results, in that order. Each carries the
        payload *and* the capture record and spill handle beside it, which is
        what makes this pair sufficient to build a result: the caller reads
        ``.payload`` for the text and hands the whole record on, so a spilled
        capture's file is still reachable after the drain has been consumed.

    A reader's own failure, and a caller's cancellation, propagate unchanged
    once the run's tasks have been reconciled.
    """
    try:
        async with asyncio.timeout(_deadline_for_collection(execution)):
            await _stop_idle_monitor(execution.idle)
            await _settle_stdin_writer(run.tasks.stdin_task)
            await _await_consumers(
                run.tasks.consumers,
                _deadline_for_collection(execution),
            )
    except TimeoutError as exc:
        stdout_drain, stderr_drain = await _settle_and_reconcile(
            run,
            pid,
            execution,
            preserve=True,
        )
        _handle_stream_timeout(
            exc,
            stdout_text=stdout_drain.payload,
            stderr_text=stderr_drain.payload,
            timeout=execution.timeout,
        )
    except BaseException:
        await _settle_and_reconcile(run, pid, execution, preserve=False)
        raise
    # Every consumer is done by the time the block above returns, so this
    # resolves immediately. It is kept as the single route the drains travel to
    # their caller, rather than a second one that could drift from the path a
    # streamed run uses.
    stdout_drain, stderr_drain = await _consumer_drains(
        run.tasks.consumers,
        _drain_context(execution, run.tasks, pid, preserve=True),
    )
    # Settled here, on the one path that read every consumer to completion: the
    # caller assembles its result from these collectors once this returns, and
    # ``snapshot()`` reports ``()`` until a settle marks the drain done. The
    # failure paths above re-raise, so no result is assembled after them and the
    # collectors they leave unsettled are read by nobody.
    for diagnostics in run.tasks.relay_diagnostics:
        diagnostics.settle()
    return stdout_drain, stderr_drain


async def _consumer_drains(
    consumers: tuple[_Consumer, _Consumer],
    context: _DrainContext,
) -> _DrainPair:
    """Read the drains of the two readers, positionally.

    The pair may hold ``None`` for a stream cuprum holds no pipe for; the
    stand-in keeps each result in its own stream's position, and decoding maps
    a settled ``None`` to that stream's own empty capture rather than to an
    absent slot — the caller reads ``.payload`` and ``.capture`` off both.

    Returns
    -------
    _DrainPair
        The stdout and stderr drain results, records included, in that order.
    """
    gathered = await asyncio.gather(*(_consumer_awaitable(task) for task in consumers))
    return _decode_consumer_drains(gathered, context=context)


async def _await_consumers(
    consumers: tuple[_Consumer, _Consumer],
    remaining: float | None,
) -> None:
    """Wait for both readers, re-raising a reader's own failure at once.

    Returns when both readers have finished normally, and raises ``TimeoutError``
    when *remaining* elapses with a reader still running — the same exception the
    enclosing scope raises for the awaits that have no bound of their own, so the
    caller has one expiry route rather than two.

    Observing the readers rather than gathering them is what protects a trailing
    line: a reader parked in ``queue.put`` on a full queue is left for the
    reconciliation to cancel, and the reconciliation keeps every prefix already
    captured.

    Returning on the first exception is what preserves failure attribution.
    Waiting for every reader instead would spend the whole budget on a sibling
    that is hanging, and the budget expiring would then be reported as the run's
    own deadline — a timeout handed to the caller in place of the reader failure
    that actually ended the run. ``FIRST_EXCEPTION`` does not short-circuit on a
    reader's own ``CancelledError``, which is deliberate: cancelling a reader
    *is* the run ending, so the wait stays until the deadline and reports the
    expiry the caller configured. When no reader fails the two conditions agree,
    and the normal path is unchanged.

    A stream cuprum holds no pipe for contributes no reader and is skipped. Its
    payload is ``None`` and its slot is restored by
    :func:`_consumer_drains`, which is the only place the positional pair is
    assembled.

    Raises
    ------
    TimeoutError
        When *remaining* elapses with a reader still unfinished. A budget that
        was already spent when the wait began is reported the same way, because
        ``asyncio.wait`` returns at once for a non-positive timeout and leaves
        every reader that has not finished in *pending*.
    """
    readers = tuple(task for task in consumers if task is not None)
    if not readers:
        return
    finished, pending = await asyncio.wait(
        readers,
        timeout=remaining,
        return_when=asyncio.FIRST_EXCEPTION,
    )
    _raise_reader_failure(finished)
    if pending:
        # No reader failed, so what is left is an unfinished reader and a
        # budget that ran out waiting for it — possibly before the wait began,
        # which is the same expiry arriving by a different route. That is the
        # run's own deadline, reported as the same exception the enclosing
        # scope raises.
        raise TimeoutError
    # Every reader finished, and every finished reader's result is discarded:
    # the drains are read once, after this function returns, on the single
    # route a completed run takes to its caller. What this call is for is the
    # remaining re-raise — a reader that ended by its own cancellation.
    await asyncio.gather(*finished)


def _raise_reader_failure(finished: set[asyncio.Task[object]]) -> None:
    """Re-raise a reader's own failure, before any expiry can be reported.

    The order matters: ``asyncio.wait`` reports readers that finished *and* a
    wait that timed out through the same call, so a sibling's failure and the
    deadline can be true at once. The failure is what ended the run, and
    reporting the deadline instead would hand the caller a timeout in place of
    the error that actually caused it.

    A reader that ended by its own cancellation is skipped rather than
    re-raised: cancelling a reader *is* the run ending, and the reconciliation
    this route falls back to is what settles it.
    """
    for task in finished:
        if task.cancelled():
            continue
        error = task.exception()
        if error is not None:
            raise error


async def _settle_and_reconcile(
    run: _LineStreamRun,
    pid: int | None,
    execution: _SubprocessExecution,
    *,
    preserve: bool,
) -> _DrainPair:
    """Settle the owned group, then reconcile every task the run still owns.

    The group goes first for the same reason it does on the streamed path: a
    descendant still holding the pipes is what stops a consumer reaching
    end-of-file, so no drain can succeed until it is gone. An inherited group
    has nothing to settle and
    :func:`~cuprum._subprocess_wait._settle_owned_group_before_draining` returns
    without signalling anything, so a run that does not own its group is never
    signalled — the rule ADR 021 sets for every other exit path.

    *preserve* selects the run's capture policies for the drain, exactly as the
    streamed path's reconciliation does: the expiry route keeps the partial
    output its ``TimeoutExpired`` will report, while the re-raising routes
    report ``(None, None)`` rather than spending the capture grace on text
    nobody will read.

    Both halves run under **one** shield. ``_shielded_cleanup`` finishes the
    task it was given and then re-raises the caller's cancellation, so two
    sequential shielded calls are not one shielded sequence: a cancellation
    arriving during the first would exit before the second was awaited, leaving
    the tasks unreconciled. Composing them into one coroutine and handing that
    to the teardown wrapper is what makes the shield cover the pair; the wrapper
    supplies the shield, so no second one is added around it.

    The wrapper is used rather than the reconciliation being called directly, so
    this route gets the same lifecycle brackets, and the same single
    reconciliation point, as every other exit path.

    Returns
    -------
    _DrainPair
        The reconciled stdout and stderr drain results, in that order.
    """
    # Imported here, not at module scope, to avoid a cycle: the coordinator
    # imports this module, and a test patches the coordinator's module-level
    # ``_drain_stream_consumers`` and ``_shielded_cleanup`` names, so the
    # teardown wrapper has to resolve them in that module's globals.
    from cuprum._line_stream.coordinator import _run_line_stream_teardown

    async def settle_then_reconcile() -> _DrainPair:
        """Settle the group, then reconcile the run's tasks, as one unit."""
        await _settle_owned_group_before_draining(run.process, execution)
        return await _reconcile_run_tasks(
            run.tasks,
            _drain_context(execution, run.tasks, pid, preserve=preserve),
        )

    return await _run_line_stream_teardown(run, settle_then_reconcile())
