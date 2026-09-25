"""Pull-style line iteration over one subprocess.

Owns the coordination behind ``SafeCmd.lines()``. The subprocess, its stream
consumers, its deadline, and the teardown rules are the same primitives
``SafeCmd.run()`` uses, reached through the same builders; the addition is a
finite :class:`asyncio.Queue` fed by a per-line callback chained ahead of the
caller's own ``on_line``, so a caller iterating lines is decoupled from the
drain without forking the run semantics.

That queue is what bounds retention. The sink awaits ``queue.put``, and the
drain loop awaits the sink, so a caller that iterates slowly pauses the read
rather than letting a chatty child accumulate events in memory.

The consumer tasks are registered in ``_RunTaskOwnership.consumers``, so the
shared reconciliation cancels and drains them exactly once on every exit
path: completion, timeout, caller ``break``, generator close, and external
cancellation alike.

This module holds the run, teardown, and coordination steps. The queue
plumbing, telemetry types, pre-return spawn machinery, and post-exit drain live
in sibling modules of the ``cuprum._line_stream`` package, which re-exports all
of them. Tests that replace a collaborator of these steps patch this module,
because each function resolves those names through this module's globals.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ
from time import perf_counter

from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._line_stream.drain import _drain_after_exit
from cuprum._line_stream.spawn import (
    _abandon_unstarted_run,
    _build_unstarted_run,
    _with_line_sink_hooks,
)
from cuprum._line_stream.telemetry import (
    _LineStreamEventDetails,
    _LineStreamTelemetry,
)
from cuprum._pipeline_types import _EventDetails
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._subprocess_execution import _spawn_subprocess, _SubprocessExecution
from cuprum._subprocess_timeout import (
    _emit_exit_event,
    _ExitEventDetails,
    _handle_stream_timeout,
    _handle_subprocess_timeout,
    _SubprocessTimeoutContext,
    _SubprocessTimeoutError,
)
from cuprum._subprocess_wait import (
    _drain_stream_consumers,
    _DrainContext,
    _reconcile_run_tasks,
    _wait_for_exit_code_within_timeout,
)
from cuprum.line_stream_events import LineStreamPhase

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._line_stream.line_queue import _LineQueueItem, _LineStreamRun
    from cuprum.sh import CommandResult

__all__ = [
    "_cleanup_failed_line_stream_run",
    "_coordinate_line_stream",
    "_discard_drain",
    "_run_line_stream_teardown",
    "_run_to_command_result",
    "_start_line_stream_run",
    "_wait_for_line_stream_exit",
]


async def _start_line_stream_run(
    execution: _SubprocessExecution,
    queue: asyncio.Queue[_LineQueueItem],
) -> _LineStreamRun:
    """Spawn the subprocess and its queue-feeding consumers.

    Mirrors the streamed-path setup in :func:`_run_subprocess_with_streams`:
    spawn, record the start reference, start the stdin writer, then start the
    consumers with capture and echo as configured.

    Every step between the spawn and the return is owned by
    :func:`_abandon_unstarted_run`, matching :func:`_spawn_pipeline_processes`.
    The ``start`` event is not exempt: it invokes synchronous observe hooks
    inline and re-raises their failures, so without that ownership a failing
    ``start`` hook leaves the child running with undrained pipes, which a caller
    that only drains observe tasks can never reclaim.

    Returns
    -------
    _LineStreamRun
        The spawned run, with the process, its task ownership, the queue,
        and the monotonic start reference.

    Whatever that setup raises propagates unchanged, once the child has been
    stopped and reaped and its tasks drained.
    """
    telemetry = _LineStreamTelemetry(
        exec_id=execution.observation.exec_id,
        queue_capacity=queue.maxsize,
    )
    # Rebound, not passed straight to the spawn: the stream consumers read
    # ``on_line`` off the execution too, so a chained hook that only reached
    # ``_spawn_subprocess`` would leave the queue sink uninstalled and starve
    # the iterator.
    execution = _with_line_sink_hooks(execution, queue, telemetry)
    process = await _spawn_subprocess(execution)
    started_at = perf_counter()
    # Rebuilt, not mutated: the stream consumers read ``started_at`` off the
    # execution when stamping each ``LineEvent``. Left at its ``0.0`` default,
    # every ``at`` would be the machine's monotonic uptime rather than seconds
    # since this command started.
    execution = dc.replace(execution, started_at=started_at)
    pid = process.pid
    run = _build_unstarted_run(process, execution, queue, telemetry)
    try:
        if execution.idle is not None:
            # Armed here, once the child is running, exactly as the streamed
            # ``run()`` path arms it: the catalogue checks and before hooks that
            # preceded this spawn are the parent's work, not the child's
            # silence.
            execution.idle.launch()
        telemetry.pid = pid
        # Emitted after the consumers exist, which is sound because creating a
        # task does not run it: nothing between that creation and this emit
        # suspends, so ``start`` is still the first thing an observer of this
        # execution sees. Owning every task before the first step that can fail
        # is what lets the reclaim path drain a whole run rather than guess at
        # how much of one exists.
        execution.observation.emit("start", _EventDetails(pid=pid))
        telemetry.emit(LineStreamPhase.SPAWNED)
    except BaseException:
        await _abandon_unstarted_run(run, execution)
        raise
    return run


async def _wait_for_line_stream_exit(
    run: _LineStreamRun,
    execution: _SubprocessExecution,
) -> tuple[int, float, str | None, str | None]:
    """Await process exit, reconciling every task when the wait fails.

    Mirrors ``_wait_for_streamed_process_exit`` so a ``lines()`` run applies
    its deadline, terminates the child, and drains its consumers exactly the
    way ``run()`` does.

    Returns
    -------
    tuple[int, float, str | None, str | None]
        The exit code, exit timestamp, and captured stdout/stderr.

    Raises
    ------
    asyncio.CancelledError
        If the caller cancels line iteration while the subprocess is running.
    """
    try:
        exit_code, exited_at = await _wait_for_exit_code_within_timeout(
            run.process,
            execution,
        )
    except TimeoutError as exc:
        run.telemetry.emit(LineStreamPhase.TIMEOUT, _LineStreamEventDetails(error=exc))
        stdout_text, stderr_text = await _cleanup_failed_line_stream_run(
            run,
            execution,
            capture=execution.capture,
        )
        _handle_stream_timeout(
            exc,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            timeout=execution.timeout,
        )
    except asyncio.CancelledError:
        run.telemetry.emit(LineStreamPhase.CANCELLED)
        await _cleanup_failed_line_stream_run(
            run,
            execution,
            capture=False,
        )
        raise
    except BaseException:
        await _cleanup_failed_line_stream_run(
            run,
            execution,
            capture=False,
        )
        raise
    # The child has exited, so silence no longer means anything: stop before
    # waiting on stream EOF, which a grandchild's inherited pipe can hold open
    # long after its parent is gone.
    await _stop_idle_monitor(execution.idle)
    stdout_text, stderr_text = await _drain_after_exit(
        run,
        run.process.pid,
        execution,
    )
    return exit_code, exited_at, stdout_text, stderr_text


async def _run_line_stream_teardown[T](
    run: _LineStreamRun,
    operation: cabc.Awaitable[T],
) -> T:
    """Run one teardown operation between its lifecycle boundaries.

    The wrapper is generic over the operation's result so each caller keeps its
    own drain policy: :func:`_cleanup_failed_line_stream_run` selects capture
    from the outcome, while :func:`_discard_drain` always discards.

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
    capture: bool,
) -> tuple[str | None, str | None]:
    """Reconcile a failed run after emitting its teardown boundaries."""
    pid = run.process.pid
    return await _run_line_stream_teardown(
        run,
        _reconcile_run_tasks(
            run.tasks,
            _DrainContext(
                capture=capture,
                pid=pid,
                observation=execution.observation,
                discard_on_cancel=run.tasks.discard_on_cancel,
            ),
        ),
    )


async def _discard_drain(
    run: _LineStreamRun,
    pid: int | None,
    execution: _SubprocessExecution,
) -> tuple[str | None, str | None]:
    """Discard and reconcile the line stream's consumers after a failure."""
    return await _run_line_stream_teardown(
        run,
        _drain_stream_consumers(
            run.tasks.consumers,
            _DrainContext(
                capture=False,
                pid=pid,
                observation=execution.observation,
                discard_on_cancel=run.tasks.discard_on_cancel,
            ),
        ),
    )


async def _coordinate_line_stream(
    run: _LineStreamRun,
    execution: _SubprocessExecution,
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

    Raises
    ------
    asyncio.CancelledError
        When the iterator cancels this coordinator as teardown. Re-raised
        rather than published, so a plain ``aclose()`` never hands the caller a
        ``CancelledError`` they did not issue.
    """
    try:
        result = await _run_to_command_result(run, execution)
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
    execution: _SubprocessExecution,
) -> CommandResult:
    """Run to exit, assemble the ``CommandResult``, and emit the exit event."""
    started_at = run.started_at
    stdout_text: str | None = None
    stderr_text: str | None = None
    try:
        (
            exit_code,
            exited_at,
            stdout_text,
            stderr_text,
        ) = await _wait_for_line_stream_exit(run, execution)
    except (TimeoutError, _SubprocessTimeoutError) as exc:
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
        stdout=stdout_text,
        stderr=stderr_text,
    )
