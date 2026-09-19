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
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ
from time import perf_counter

from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._line_callbacks import _chain_line_hooks
from cuprum._pipeline_types import _EventDetails
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._subprocess_execution import (
    _spawn_subprocess,
    _SubprocessExecution,
)
from cuprum._subprocess_stdin import _spawn_stdin_writer
from cuprum._subprocess_streams import (
    _build_stream_config,
    _spawn_stream_consumers,
)
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
    _RunTaskOwnership,
    _wait_for_exit_code_within_timeout,
)
from cuprum.line_stream_events import (
    LineStreamEvent,
    LineStreamPhase,
    LineStreamSink,
)
from cuprum.line_stream_observation import _emit_line_stream_event
from cuprum.lines import LineEvent, LineStreamName

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecId
    from cuprum.lines import _LineHookFn
    from cuprum.sh import CommandResult

# The queue item is a line event while the run streams, and the run's
# ``CommandResult`` exactly once, when the coordinator finishes; the result
# doubles as the sentinel that ends iteration.
type _LineQueueItem = LineEvent | CommandResult

# Finite on purpose. The sink awaits ``queue.put``, so a caller iterating
# slowly stops the consumers reading the pipe instead of letting a chatty child
# grow the queue without bound.
_LINE_QUEUE_CAPACITY = 256


@dc.dataclass(frozen=True, slots=True)
class _LineStreamEventDetails:
    """Optional bounded fields attached to one line-stream lifecycle event."""

    stream: LineStreamName | None = None
    sink: LineStreamSink | None = None
    error: BaseException | None = None
    queue_size: int | None = None


@dc.dataclass(slots=True)
class _LineStreamTelemetry:
    """Emit correlated, bounded lifecycle details for one line-stream run."""

    exec_id: ExecId
    queue_capacity: int
    pid: int | None = None
    is_queue_saturated: bool = False

    def emit(
        self,
        phase: LineStreamPhase,
        details: _LineStreamEventDetails | None = None,
    ) -> None:
        """Publish one lifecycle boundary without carrying decoded text."""
        event_details = details or _LineStreamEventDetails()
        _emit_line_stream_event(
            LineStreamEvent(
                phase=phase,
                exec_id=self.exec_id,
                pid=self.pid,
                stream=event_details.stream,
                sink=event_details.sink,
                queue_size=event_details.queue_size,
                queue_capacity=(
                    self.queue_capacity
                    if event_details.queue_size is not None
                    else None
                ),
                error_type=(
                    type(event_details.error).__name__
                    if event_details.error is not None
                    else None
                ),
            )
        )

    def report_queue_saturation(
        self,
        queue: asyncio.Queue[_LineQueueItem],
        event: LineEvent,
    ) -> None:
        """Report a transition into bounded queue backpressure."""
        if queue.full() and not self.is_queue_saturated:
            self.is_queue_saturated = True
            self.emit(
                LineStreamPhase.QUEUE_SATURATED,
                _LineStreamEventDetails(
                    stream=event.stream,
                    queue_size=queue.qsize(),
                ),
            )


@dc.dataclass(frozen=True, slots=True)
class _LineStreamRun:
    """The spawned pieces one ``lines()`` iteration coordinates.

    Attributes
    ----------
    process:
        The subprocess whose stdout and stderr feed the queue.
    tasks:
        Ownership of the stdin writer and the stream consumers, so the shared
        reconciliation cancels and drains each exactly once.
    queue:
        The queue every ``LineEvent`` is posted to; the sentinel ends
        iteration.
    started_at:
        Monotonic reference the per-line ``at`` stamps are measured from.

    """

    process: asyncio.subprocess.Process
    tasks: _RunTaskOwnership
    queue: asyncio.Queue[_LineQueueItem]
    started_at: float
    telemetry: _LineStreamTelemetry


def _line_event_queue() -> asyncio.Queue[_LineQueueItem]:
    """Return the finite queue one ``lines()`` iteration consumes."""
    return asyncio.Queue(maxsize=_LINE_QUEUE_CAPACITY)


def _queue_line_sink(
    queue: asyncio.Queue[_LineQueueItem],
    telemetry: _LineStreamTelemetry | None = None,
) -> _LineHookFn:
    """Return an asynchronous hook that posts each ``LineEvent`` to the queue.

    Asynchronous rather than a synchronous ``put_nowait`` because the queue is
    finite: a full queue parks the stream consumer until the iterator drains a
    slot, so ``lines()`` applies backpressure to the child instead of dropping
    events or retaining them without limit. A synchronous sink could only
    raise ``asyncio.QueueFull`` out of the drain loop.

    Returns
    -------
    _LineHookFn
        The hook that posts one event to *queue*, awaiting a free slot when the
        queue is full.
    """

    async def enqueue(event: LineEvent) -> None:
        """Post one stamped line to the consumer queue."""
        if telemetry is not None:
            telemetry.report_queue_saturation(queue, event)
        await queue.put(event)
        if telemetry is not None:
            telemetry.is_queue_saturated = queue.full()

    return enqueue


def _observed_line_hook(
    hook: _LineHookFn,
    sink: LineStreamSink,
    telemetry: _LineStreamTelemetry,
) -> _LineHookFn:
    """Wrap one delivery hook so its failure is correlated before it escapes."""

    async def deliver(event: LineEvent) -> None:
        """Deliver one event while preserving the hook's failure semantics."""
        try:
            outcome = hook(event)
            if outcome is not None:
                await outcome
        except asyncio.CancelledError:
            telemetry.emit(
                LineStreamPhase.CANCELLED,
                _LineStreamEventDetails(stream=event.stream, sink=sink),
            )
            raise
        except BaseException as error:
            telemetry.emit(
                LineStreamPhase.SINK_FAILED,
                _LineStreamEventDetails(
                    stream=event.stream,
                    sink=sink,
                    error=error,
                ),
            )
            raise

    return deliver


async def _start_line_stream_run(
    execution: _SubprocessExecution,
    queue: asyncio.Queue[_LineQueueItem],
) -> _LineStreamRun:
    """Spawn the subprocess and its queue-feeding consumers.

    Mirrors the streamed-path setup in :func:`_run_subprocess_with_streams`:
    spawn, record the start reference, start the stdin writer, then start the
    consumers with capture and echo as configured.

    Returns
    -------
    _LineStreamRun
        The spawned run, with the process, its task ownership, the queue,
        and the monotonic start reference.
    """
    # Frozen dataclass: the stamped execution carries the start reference the
    # composed callbacks read, and the caller's ``on_line`` chained ahead of
    # this driver's queue sink, so it is rebuilt rather than mutated.
    telemetry = _LineStreamTelemetry(
        exec_id=execution.observation.exec_id,
        queue_capacity=queue.maxsize,
    )
    hooks: list[_LineHookFn] = []
    if execution.on_line is not None:
        hooks.append(_observed_line_hook(execution.on_line, "callback", telemetry))
    hooks.append(
        _observed_line_hook(_queue_line_sink(queue, telemetry), "queue", telemetry)
    )
    execution = dc.replace(execution, on_line=_chain_line_hooks(hooks))
    process = await _spawn_subprocess(execution)
    started_at = perf_counter()
    execution = dc.replace(execution, started_at=started_at)
    if execution.idle is not None:
        # Armed here, once the child is running, exactly as the streamed
        # ``run()`` path arms it: the catalogue checks and before hooks that
        # preceded this spawn are the parent's work, not the child's silence.
        execution.idle.launch()
    pid = process.pid
    telemetry.pid = pid
    execution.observation.emit("start", _EventDetails(pid=pid))
    telemetry.emit(LineStreamPhase.SPAWNED)
    discard_on_cancel = asyncio.Event()
    stream_config = _build_stream_config(execution, discard_on_cancel)
    tasks = _RunTaskOwnership(
        stdin_task=_spawn_stdin_writer(
            process, execution.stdin_data, execution.observation
        ),
        # The same consumer builder ``run()`` uses, so iterating lines can
        # never silently diverge from it on capture, echo, or sink selection.
        consumers=_spawn_stream_consumers(
            process,
            execution,
            stream_config,
            pid=pid,
        ),
        discard_on_cancel=discard_on_cancel,
        idle=execution.idle,
    )
    return _LineStreamRun(
        process=process,
        tasks=tasks,
        queue=queue,
        started_at=started_at,
        telemetry=telemetry,
    )


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


async def _drain_after_exit(
    run: _LineStreamRun,
    pid: int | None,
    execution: _SubprocessExecution,
) -> tuple[str | None, str | None]:
    """Await the settled consumers and drain them exactly once on failure."""
    if run.tasks.stdin_task is not None:
        try:
            await run.tasks.stdin_task
        except BaseException:
            await _discard_drain(run, pid, execution)
            raise
    try:
        return await asyncio.gather(*run.tasks.consumers)
    except BaseException:
        await _discard_drain(run, pid, execution)
        raise


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
