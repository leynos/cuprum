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
post-exit draining live in sibling modules and are re-exported by the package.
Tests patch this module because its coordination functions resolve
collaborators through this module's globals.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._capture_cleanup import (
    _cleanup_capture_sinks,
    _finalize_capture_handles,
)
from cuprum._delivery_lifecycle import _close_delivery_worker
from cuprum._line_stream.drain import _collect_line_stream_after_exit
from cuprum._line_stream.telemetry import _LineStreamEventDetails
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._result_assembly import _recorded_captures, _require_text
from cuprum._stream_drain import _drain_stream_consumers
from cuprum._subprocess_execution import _relay_fallbacks_for_result
from cuprum._subprocess_timeout import (
    _emit_exit_event,
    _ExitEventDetails,
    _handle_stream_timeout,
    _handle_subprocess_timeout,
    _SubprocessTimeoutContext,
    _SubprocessTimeoutError,
)
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
    from cuprum._subprocess_wait_types import _DrainPair, _StreamPayload
    from cuprum.capture_policy import CapturedStream
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
    """Await process exit, reconciling every task when the wait fails.

    Mirrors ``_wait_for_streamed_process_exit`` so a ``lines()`` run applies
    its deadline, terminates the child, and drains its consumers exactly the
    way ``run()`` does.

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
    """
    execution = run.execution
    try:
        exit_code, exited_at = await _await_exit_or_writer_failure(
            _wait_for_exit_code_within_timeout(run.process, execution),
            run.tasks.stdin_task,
        )
    except TimeoutError as exc:
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


async def _finalize_line_stream_captures(
    run: _LineStreamRun,
    stdout_drain: _CapturedDrain,
    stderr_drain: _CapturedDrain,
) -> None:
    """Close the spill files a completed ``lines()`` run still owns.

    ``run()`` closes its handles right after collecting the streams, and the
    ``lines()`` success path reaches the same place — every consumer has
    settled and the drain results are in hand — so it owes the same removal.
    Without this, a ``FileBackedCapture`` would return a record reporting
    ``owned=True`` for a file that outlives the run, with nothing left to
    remove it.

    Shielded because the drains are already settled: a cancellation landing
    between the collect and the close would leave the file on disk while the
    caller was still about to be handed the result.
    """
    await _shielded_cleanup(
        _finalize_capture_handles(
            (stdout_drain, stderr_drain),
            run.tasks.captures,
        )
    )


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
    execution = run.execution
    # The run's own bundle is also the instant the duration and the exit event
    # are measured from, and it only ever exists on the execution the spawn
    # helper stamped.
    started_at = execution.started_at
    stdout_text: _StreamPayload | None = None
    stderr_text: _StreamPayload | None = None
    captures: tuple[CapturedStream | None, CapturedStream | None] = (None, None)
    try:
        (
            exit_code,
            exited_at,
            stdout_drain,
            stderr_drain,
        ) = await _wait_for_line_stream_exit(run)
        stdout_text = stdout_drain.payload
        stderr_text = stderr_drain.payload
        captures = (stdout_drain.capture, stderr_drain.capture)
        await _finalize_line_stream_captures(run, stdout_drain, stderr_drain)
    except _SubprocessTimeoutError as exc:
        # Only the internal wrapper is translated here. Every genuine expiry in
        # the exit helper has already been through ``_handle_stream_timeout``,
        # which raises that wrapper; a bare ``TimeoutError`` escaping it is a
        # reader's own failure — an ``on_line`` hook using ``asyncio.timeout``
        # internally, say — and must reach the caller unchanged rather than be
        # reported as this run's deadline.
        _handle_subprocess_timeout(
            _SubprocessTimeoutContext(
                execution=execution,
                process=run.process,
                started_at=started_at,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
            ),
            exc,
        )

    await _shielded_cleanup(
        _close_delivery_worker(
            execution.delivery_worker,
            drain_grace_s=execution.delivery_grace_s,
        ),
    )

    run.telemetry.emit(LineStreamPhase.COMPLETED)
    _emit_exit_event(
        execution.observation,
        _ExitEventDetails(
            pid=run.process.pid,
            exit_code=exit_code,
            started_at=started_at,
            exited_at=exited_at,
        ),
    )
    from cuprum._subprocess_context import _sh_module

    return _sh_module().CommandResult(
        program=execution.cmd.program,
        argv=execution.cmd.argv,
        exit_code=exit_code,
        pid=run.process.pid if run.process.pid is not None else -1,
        stdout=_require_text(stdout_text, "stdout"),
        stderr=_require_text(stderr_text, "stderr"),
        relay_fallbacks=_relay_fallbacks_for_result(run.tasks.relay_diagnostics),
        resolved_path=execution.observation.resolved_path,
        captures=_recorded_captures(captures),
    )
