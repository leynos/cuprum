"""Pull-style line iteration over one subprocess.

``SafeCmd.lines()`` uses the same subprocess, deadline, and teardown
primitives as ``SafeCmd.run()``. Its finite queue is fed by a per-line hook
ahead of the caller's ``on_line`` callback, keeping iteration decoupled without
changing run semantics.

The sink awaits ``queue.put`` and the drain awaits that sink. A slow iterator
therefore pauses reads instead of retaining unbounded output. Consumer tasks
belong to ``_RunTaskOwnership.consumers`` and are reconciled once on completion,
timeout, caller ``break``, generator close, and external cancellation.

This module holds the exit wait, the teardown steps, and the coordination that
turns a settled run into a result. Queue plumbing, telemetry, spawn setup, and
post-exit draining live in sibling modules and are re-exported by the package,
as is the result assembly that follows a clean exit. Tests patch this module
because its coordination functions resolve collaborators through this module's
globals.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._capture_cleanup import _cleanup_capture_sinks
from cuprum._line_stream.drain import _collect_line_stream_after_exit
from cuprum._line_stream.result_assembly import _assemble_line_stream_result
from cuprum._line_stream.telemetry import _LineStreamEventDetails
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._stream_drain import _drain_stream_consumers
from cuprum._subprocess_rendezvous import _stdin_writer_failure
from cuprum._subprocess_timeout import _handle_stream_timeout
from cuprum._subprocess_wait import (
    _await_exit_or_writer_failure,
    _drain_context,
    _reconcile_run_tasks,
    _wait_for_exit_code_within_timeout,
)
from cuprum.line_stream_events import LineStreamPhase

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._line_stream.line_queue import _LineQueueItem, _LineStreamRun
    from cuprum._stream_drain_finish import _CapturedDrain
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_wait_types import _DrainPair
    from cuprum.sh import CommandResult

__all__ = [
    "_cleanup_failed_line_stream_run",
    "_coordinate_line_stream",
    "_discard_drain",
    "_run_line_stream_teardown",
    "_run_to_command_result",
    "_wait_for_line_stream_exit",
]


async def _wait_for_line_stream_exit(
    run: _LineStreamRun,
) -> tuple[int, float, _CapturedDrain, _CapturedDrain]:
    """Await process exit, then collect the drains under the remaining budget.

    Mirrors ``_wait_for_streamed_process_exit`` so a ``lines()`` run applies
    its deadline, terminates the child, and drains its consumers exactly the
    way ``run()`` does. The wait and its failure reconciliation belong to
    :func:`_await_line_stream_exit`; what stays here is the post-exit
    collection that follows a clean exit.

    The execution bundle is read from the run rather than accepted alongside
    it. The spawn helper rebuilds that bundle with the run's monotonic start
    reference stamped on, and the post-exit deadline is a remainder measured
    from it, so a second bundle would let this function time the child from a
    ``started_at`` other than the one the spawn actually recorded.

    Returns
    -------
    tuple[int, float, _CapturedDrain, _CapturedDrain]
        The exit code, exit timestamp, and the stdout and stderr drain
        results, in that order. The records travel with the payloads so the
        caller reads how each policy retained its stream without draining a
        second time.

    Raises
    ------
    asyncio.CancelledError
        If the caller cancels line iteration while the subprocess is running.
    TimeoutError
        When the run's configured deadline expires. A stdin writer that failed
        with a ``TimeoutError`` of its own raises that same exception too,
        reconciled but untranslated: it is the producer's error, not this
        run's deadline.
    """  # ruff: ignore[docstring-extraneous-exception] - both propagate from the called helpers
    execution = run.execution
    exit_code, exited_at = await _await_line_stream_exit(run, execution)
    # The child has exited, so silence no longer means anything; the heartbeat
    # stops inside the bounded collection below, along with the writer wait and
    # the consumers, because a grandchild's inherited pipe can hold that read
    # open long after its parent is gone and every one of the three would then
    # wait for ever on a deadline the exit wait has already spent.
    stdout_drain, stderr_drain = await _collect_line_stream_after_exit(
        run,
        run.process.pid,
        execution,
    )
    return exit_code, exited_at, stdout_drain, stderr_drain


async def _await_line_stream_exit(
    run: _LineStreamRun,
    execution: _SubprocessExecution,
) -> tuple[int, float]:
    """Await the child's exit, reconciling the run on every failure route.

    The reconciliation is what makes each route terminal: the caller sees the
    outcome only once every task the run still owns has been settled, so a
    failure re-raised here never leaves a reader or writer behind.

    Returns
    -------
    tuple[int, float]
        The exit code and exit timestamp.

    Raises
    ------
    asyncio.CancelledError
        If the caller cancels line iteration while the subprocess is running.
    TimeoutError
        When the run's configured deadline expires, or when a stdin writer
        failed with a ``TimeoutError`` of its own — reconciled but
        untranslated, because that is the producer's error.
    """
    try:
        return await _await_exit_or_writer_failure(
            _wait_for_exit_code_within_timeout(run.process, execution),
            run.tasks.stdin_task,
        )
    except TimeoutError as exc:
        # A ``TimeoutError`` can arrive here two ways, and only one of them is
        # this run's deadline. The exit wait reports its own expiry as this
        # type; so would a stdin writer that failed with one, because the
        # rendezvous above re-raises a failed writer unchanged and
        # ``TimeoutError`` is an ``OSError`` subclass. Identity is what tells
        # them apart, and it is exact: the rendezvous raises the very object
        # ``_stdin_writer_failure`` reads back off the task. Translating the
        # writer's failure instead would report a deadline the caller never
        # configured, which ``_handle_stream_timeout`` rejects as an invariant
        # violation, so the caller would lose the producer's real error.
        if _stdin_writer_failure(run.tasks.stdin_task) is exc:
            await _cleanup_failed_line_stream_run(
                run,
                execution,
                preserve=False,
            )
            raise
        run.telemetry.emit(LineStreamPhase.TIMEOUT, _LineStreamEventDetails(error=exc))
        stdout_drain, stderr_drain = await _cleanup_failed_line_stream_run(
            run,
            execution,
            preserve=True,
        )
        _handle_stream_timeout(
            exc,
            stdout_text=stdout_drain.payload,
            stderr_text=stderr_drain.payload,
            timeout=execution.timeout,
        )
    except asyncio.CancelledError:
        run.telemetry.emit(LineStreamPhase.CANCELLED)
        await _cleanup_failed_line_stream_run(
            run,
            execution,
            preserve=False,
        )
        raise
    except BaseException:
        await _cleanup_failed_line_stream_run(
            run,
            execution,
            preserve=False,
        )
        raise


async def _run_line_stream_teardown[T](
    run: _LineStreamRun,
    operation: cabc.Awaitable[T],
) -> T:
    """Run one teardown operation between its lifecycle boundaries.

    The wrapper is generic over the operation's result so each caller keeps its
    own drain policy: :func:`_cleanup_failed_line_stream_run` selects
    preservation from the outcome, while :func:`_discard_drain` always
    discards.

    Returns
    -------
    T
        The operation's result, unchanged.

    Raises
    ------
    BaseException
        Whatever the operation raised, re-raised unchanged. A failed teardown
        never reports completion, so the started boundary is not paired with a
        success it did not reach.
    """  # ruff: ignore[docstring-extraneous-exception] - the operation's failure propagates through this helper
    # Release a queue putter before cancelling its stream consumer. That lets
    # the drain finish preserving raw capture while teardown is the only path
    # allowed to stop line publication.
    run.settlement_started.set()
    run.telemetry.emit(LineStreamPhase.TEARDOWN_STARTED)
    result = await _shielded_cleanup(operation)
    run.telemetry.emit(LineStreamPhase.TEARDOWN_COMPLETED)
    return result


async def _cleanup_failed_line_stream_run(
    run: _LineStreamRun,
    execution: _SubprocessExecution,
    *,
    preserve: bool,
) -> _DrainPair:
    """Reconcile a failed run after emitting its teardown boundaries.

    *preserve* selects the run's capture policies for the drain. A deadline
    expiry keeps the partial output its ``TimeoutExpired`` will report, while
    the re-raising routes ask for ``(None, None)`` — the pair that says no
    stream is being preserved — rather than spending the capture grace on text
    nobody will read.

    Returns
    -------
    _DrainPair
        The reconciled stdout and stderr drain results, in that order.
    """
    pid = run.process.pid
    return await _run_line_stream_teardown(
        run,
        _reconcile_run_tasks(
            run.tasks,
            _drain_context(execution, run.tasks, pid, preserve=preserve),
        ),
    )


async def _discard_drain(
    run: _LineStreamRun,
    pid: int | None,
    execution: _SubprocessExecution,
) -> _DrainPair:
    """Discard and reconcile the line stream's consumers after a failure.

    Unlike :func:`_cleanup_failed_line_stream_run`, this path drains the
    consumers directly rather than through ``_reconcile_run_tasks``, so it
    owes the accumulator release itself. The context captures neither stream,
    so a cancelled consumer re-raises from ``_finish_drain`` without
    finalizing: its spill handle reaches no result, and the sink is the only
    remaining reference to the file. Releasing them in ``finally`` keeps that
    true even when the drain itself raises.

    Returns
    -------
    _DrainPair
        The stdout and stderr drain results, in that order.
    """

    async def drain_then_release() -> _DrainPair:
        """Drain the consumers, then release the sinks they were reading into."""
        try:
            return await _drain_stream_consumers(
                run.tasks.consumers,
                _drain_context(execution, run.tasks, pid, preserve=False),
            )
        finally:
            await _cleanup_capture_sinks(run.tasks.captures)

    return await _run_line_stream_teardown(run, drain_then_release())


async def _coordinate_line_stream(
    run: _LineStreamRun,
    queue: asyncio.Queue[_LineQueueItem],
    result_future: asyncio.Future[CommandResult],
) -> None:
    """Drive the run to completion, post the sentinel, then resolve the result.

    Every failure path still reconciles the process and the stream tasks
    through the shared helpers before the error is published on the future,
    so the consuming iterator never waits on work that has already ended.

    The result is the sentinel that ends iteration, and it is queued before it
    is published, so a waiter that wins the race on the future can still read
    it off the queue.

    The execution bundle is taken from the run, not accepted alongside it: see
    :func:`_wait_for_line_stream_exit` for why a second source of that value is
    the defect this module exists to avoid.

    Raises
    ------
    asyncio.CancelledError
        When the iterator cancels this coordinator as teardown. Re-raised
        rather than published, so a plain ``aclose()`` never hands the caller a
        ``CancelledError`` they did not issue.
    """
    try:
        result = await _run_to_command_result(run)
    except asyncio.CancelledError:
        # Teardown, never a caller-requested cancellation: the iterator cancels
        # this coordinator only after it has stopped consuming. Re-raised so the
        # task ends cancelled instead of publishing a ``CancelledError`` the
        # caller would meet as an exception from a plain ``aclose()``.
        run.telemetry.emit(LineStreamPhase.CANCELLED)
        raise
    except BaseException as error:  # ruff: ignore[blind-except] - any failure must reach the consumer
        result_future.set_exception(error)
        return
    # Awaited, not ``put_nowait``: the queue is finite, and a caller that has
    # paused mid-iteration can leave it full. Dropping the result would strand
    # the iterator, so the post waits for the slot the ``break`` path ends by
    # cancelling this coordinator.
    await queue.put(result)
    result_future.set_result(result)


async def _run_to_command_result(
    run: _LineStreamRun,
) -> CommandResult:
    """Run to exit, assemble the ``CommandResult``, and emit the exit event.

    Always text: ``lines()`` is a text-mode driver and has no byte-exact entry
    point, so the run reaching here has a text config. The drain still reports
    the widened payload its shared signature promises, so the text guarantee
    is re-taken rather than assumed.

    Returns
    -------
    CommandResult
        The completed run's exit code, captured text, and relay diagnostics.
    """
    return await _assemble_line_stream_result(run, _wait_for_line_stream_exit)
