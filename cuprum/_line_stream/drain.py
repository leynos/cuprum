"""Drain a line stream's consumers once the child has exited.

Part of the ``cuprum._line_stream`` package. This module owns the single
post-exit collect step, bounded by whatever remains of the run's configured
timeout; the failure-path reconciliation it falls back to lives in
``cuprum._line_stream.coordinator`` because a test patches that module's
``_drain_stream_consumers`` name directly, and the reader wait the collect
bounds is ``cuprum._line_stream.reader_wait``'s.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._line_stream.reader_wait import _await_consumers
from cuprum._line_stream.telemetry import _LineStreamEventDetails
from cuprum._stream_drain import _decode_consumer_drains
from cuprum._subprocess_rendezvous import _stdin_writer_failure
from cuprum._subprocess_stdin_reclaim import (
    _await_task_to_completion,
    _settle_stdin_writer,
)
from cuprum._subprocess_stream_post_exit import _settle_owned_group_before_draining
from cuprum._subprocess_timeout import _handle_stream_timeout
from cuprum._subprocess_wait import (
    _consumer_awaitable,
    _deadline_for_collection,
    _drain_context,
    _reconcile_run_tasks,
)
from cuprum.line_stream_events import LineStreamPhase

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

    The consumers are observed with ``asyncio.wait`` rather than gathered, so a
    reader parked in ``queue.put`` on a full queue is cancelled by the
    reconciliation — which keeps every prefix already captured — rather than
    gathered, which would lose a trailing line. The writer is awaited under the
    same scope but settles inside it, so an ``INHERIT`` run, which has no group
    signal to release the writer, does not spend the whole budget on it.

    Only the run's own budget expiring becomes the translated ``TimeoutExpired``
    its caller is owed; a caller's cancellation, a reader's own failure, or any
    other post-exit error discards instead.

    Returns
    -------
    _DrainPair
        The stdout and stderr drain results, in that order. Each carries the
        payload *and* the capture record and spill handle beside it, which is
        what makes this pair sufficient to build a result: the caller reads
        ``.payload`` for the text and hands the whole record on, so a spilled
        capture's file is still reachable after the drain has been consumed.

    Raises
    ------
    TimeoutError
        When this run's own configured budget expires during the collect, and
        only then. A reader's own failure, a stdin writer's own
        ``TimeoutError``, and a caller's cancellation propagate unchanged once
        the run's tasks have been reconciled.
    """
    reader_failure: BaseException | None = None
    deadline = asyncio.timeout(_deadline_for_collection(execution))
    try:
        async with deadline:
            await _stop_idle_monitor(execution.idle)
            await _await_writer_within_scope(run)
            reader_failure = await _await_consumers(
                run.tasks.consumers,
                _deadline_for_collection(execution),
            )
    except TimeoutError as exc:
        # A reader's own failure is returned rather than raised, so a reader
        # cannot reach this arm. The writer's awaits report unchanged, and
        # ``TimeoutError`` is an ``OSError`` subclass, so one it raised would
        # otherwise be read as this run's deadline. The settle hands back the
        # writer's own exception object, so identity — not the type — says
        # which of the two this is. The scope's ``expired()`` is consulted
        # first and wins: when it fired, the deadline was spent whether or not
        # the writer happened to be failing as well, and the expiry is what a
        # caller who configured ``timeout=`` is owed. It is also what keeps
        # the spent-budget report ``_await_consumers`` raises distinct, since
        # that route leaves no writer failure behind to point at.
        writer_failure = _stdin_writer_failure(run.tasks.stdin_task)
        if not deadline.expired() and writer_failure is exc:
            await _settle_and_reconcile(run, pid, execution, preserve=False)
            raise
        await _expire_line_stream(run, pid, execution, exc)
    except BaseException:
        await _settle_and_reconcile(run, pid, execution, preserve=False)
        raise
    if reader_failure is not None:
        # Raised only after the timeout scope has closed, so a reader whose own
        # failure happens to be a ``TimeoutError`` — an ``on_line`` hook using
        # ``asyncio.timeout`` internally, say — is never mistaken for this
        # run's deadline. Reporting it as one would emit the TIMEOUT phase,
        # reconcile in capture mode, and, with no ``timeout=`` configured,
        # replace the caller's real error with an invariant failure.
        await _settle_and_reconcile(run, pid, execution, preserve=False)
        raise reader_failure
    return await _read_consumed_drains(run, pid, execution)


async def _read_consumed_drains(
    run: _LineStreamRun,
    pid: int | None,
    execution: _SubprocessExecution,
) -> _DrainPair:
    """Read the drains of consumers that have already been awaited to completion.

    Reached only on the settle path, after :func:`_collect_line_stream_after_exit`
    has observed every consumer finish, so the gather below resolves at once. It
    is kept as the single route the drains travel to their caller rather than a
    second one that could drift from the path a streamed run uses.

    Returns
    -------
    _DrainPair
        The stdout and stderr drain results, records included, in that order.
    """
    stdout_drain, stderr_drain = await _consumer_drains(
        run.tasks.consumers,
        _drain_context(execution, run.tasks, pid, preserve=True),
    )
    # Settled here, on the one path that read every consumer to completion: the
    # caller assembles its result from these collectors once this returns, and
    # ``snapshot()`` reports ``()`` until a settle marks the drain done.
    for diagnostics in run.tasks.relay_diagnostics:
        diagnostics.settle()
    return stdout_drain, stderr_drain


async def _expire_line_stream(
    run: _LineStreamRun,
    pid: int | None,
    execution: _SubprocessExecution,
    exc: TimeoutError,
) -> typ.NoReturn:
    """Settle and report a line stream whose post-exit budget expired.

    The phase is emitted before the settlement, so an observer sees the order
    the exit wait's expiry produces: the deadline, then teardown started and
    completed. Emitting after it would describe a run that had already been
    reclaimed as one that timed out while still being torn down.

    The expiry is reported with the *captured* text, because a capturing run's
    output is what the resulting ``TimeoutExpired`` reports to its caller. The
    return type is ``NoReturn`` for that reason: the refusal to return is what
    keeps the caller's ``except`` arm from falling through to the success path.
    """
    run.telemetry.emit(
        LineStreamPhase.TIMEOUT,
        _LineStreamEventDetails(error=exc),
    )
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


async def _await_writer_within_scope(run: _LineStreamRun) -> None:
    """Let the stdin writer finish, under the scope the caller already opened.

    Shielded for latency rather than correctness. Awaiting the writer task makes
    it this await's ``_fut_waiter``, and ``Task.cancel()`` forwards, so without
    the shield the deadline would be noticed only once the writer had finished
    dying — including the close grace it bounds. The shield returns at once and
    leaves reclaiming the writer to the expiry route, which is where the group
    teardown runs too.

    The shielded await is a settle rather than a bare await of the task, and it
    is the settle that supplies the bound the shield only protects. A bare await
    of a lone task has the *caller's* expiry as its only limit, so a producer
    parked in ``anext`` would hold this line until the run's deadline and then
    be reclaimed by the expiry route — correct, but only after spending the
    whole budget on a writer that could have been reclaimed one grace window
    after exit. The settle window plus the cancel it falls back to reclaims the
    task inside the scope, which is what an ``INHERIT`` run depends on: it has
    no group signal to release the writer.

    A shielded await is only safe while the writer stays owned by the run's
    task bundle, which it is: the reconciliation this route falls back to
    cancels it through ``_cancel_stdin_writer``. Nothing here cancels it, so
    there is no window in which the shield's inner task is left unowned.

    The *settle* needs the same care for a different reason. Handing
    ``asyncio.shield`` a bare coroutine makes it build a task this frame never
    holds, and ``shield`` removes its done callback from that task when the
    outer future is cancelled. A settle that then fails — the writer failing
    inside the grace window is the ordinary way it does — has nothing left to
    retrieve it, and the loop reports an unretrieved task exception against a
    run that has already ended. Creating the task here keeps it reachable, and
    the await goes through
    :func:`~cuprum._subprocess_stdin_reclaim._await_task_to_completion`, which
    retrieves the outcome whichever side of the cancellation it lands on.

    That last part is not an alternative spelling of "retain the task and
    shield it". From Python 3.14 ``asyncio.shield`` re-registers its own
    reporter, ``asyncio.tasks._log_on_exception``, on a still-pending inner
    task whenever the awaiting future is cancelled. That report is emitted
    through the loop's exception handler without consulting whether the outcome
    was retrieved, so retaining the task alone leaves it firing on 3.14 while
    3.12 and 3.13 stay silent. ``asyncio.wait`` registers no such reporter. A
    failure arriving before the cancellation still reaches this caller
    unchanged.
    """
    if run.tasks.stdin_task is None:
        return
    settle = asyncio.ensure_future(_settle_stdin_writer(run.tasks.stdin_task))
    await _await_task_to_completion(settle)


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
    :func:`~cuprum._subprocess_stream_post_exit._settle_owned_group_before_draining`
    returns without signalling anything, so a run that does not own its group
    is never signalled — the rule ADR 021 sets for every other exit path.

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
