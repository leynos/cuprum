"""Racing a child's exit against the tasks that feed or drain it.

Ending a run divides in three, and this module owns the middle: the child's own
deadline is ``cuprum._subprocess_deadline``'s, and the parent's task
reconciliation is ``cuprum._subprocess_wait``'s, but the decision to end a run
*because one of its stream tasks died* belongs to neither. Neither the child
nor the parent's task set is at fault, so neither module is the right place to
notice it.

Awaiting the child first looks correct, and is, as long as a task that fails
also stops the child. It does not. A writer's teardown closes the pipe, so a
child that reads to EOF and then works — or one that never reads — keeps
running after the failure, and the caller is told the child was slow when its
input source was already dead. A stream *consumer* fails from the other end: a
raising ``on_line`` callback ends the task draining the output, nothing reads
the pipe, and a child that keeps writing blocks on a full pipe rather than
exiting — so the exit the coordinator waits for never arrives.

The resolution is deliberately narrow. A writer that *finishes* first, or a
consumer that reaches end of stream, is the ordinary case, and the run
continues to the child's exit as it always did. A task that *failed* is an
outcome, and so is a consumer that ended cancelled or returned while preserving
a partial capture: in either case its pipe is undrained, so waiting for the
exit would hang exactly as the #551 failure did. The exit wait is then
cancelled, which is itself the termination: ``_wait_for_exit_code``'s
cancellation handler escalates through ``_terminate_all_shielded``. No
termination policy is restated here.

The precedence between the early-failure sources is state-based and fixed, so a
run with several failing tasks reports the same one every time: a settled exit
wins outright, then the stdin writer, then the stream consumers in
stdout-then-stderr order. The consumer that failed first is *not* tracked,
because ``asyncio.wait`` reports a set rather than an order; the stream order
is the deterministic choice instead. What a settled consumer's outcome *means*,
and the post-exit wait that classifies it, is
``cuprum._subprocess_task_outcomes``'s; this module only *selects* among the
outcomes those helpers report. Settling the tasks it reports on is its callers'
work, through the reconciliation each already performs.

The precedence is read twice, and the second read is the writer's alone. A
stdin writer raises before its cleanup releases the pipe, so it can still be
running while a consumer has already failed, and terminating the child is what
lets it finish. The consumers are not asked again: termination makes the pipes
reach EOF, a strict decoder then flushes a truncated sequence, and that
artefact would replace the callback failure already selected.

A writer's second read alone is not enough, because that window has two ends. A
writer that has already raised is *still running* while it unwinds — its
producer's finalizer is the caller's code — so a read taken then sees a task
that has not failed. The writer therefore publishes its failure before it
starts unwinding, and precedence reads that publication before the task.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._subprocess_task_outcomes import (
    _await_consumer_outcomes as _await_consumer_outcomes,
)
from cuprum._subprocess_task_outcomes import (
    _consumer_failure as _consumer_failure,
)
from cuprum._subprocess_task_outcomes import (
    _stdin_writer_failure as _stdin_writer_failure,
)
from cuprum._subprocess_task_outcomes import (
    _StdinWriterLog as _StdinWriterLog,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _writer_failure(
    stdin_task: asyncio.Task[object] | None,
    log: _StdinWriterLog | None,
) -> BaseException | None:
    """Return the writer's published failure, else the one its task carries."""
    if log is not None and log.error is not None:
        return log.error
    return _stdin_writer_failure(stdin_task)


def _first_early_failure(
    stdin_task: asyncio.Task[object] | None,
    consumers: cabc.Sequence[asyncio.Task[object] | None],
    *,
    stdin_log: _StdinWriterLog | None = None,
) -> BaseException | None:
    """Select the failure the run reports, in the fixed precedence order.

    Two statements rather than one expression, so the order is stated once: the
    stdin writer outranks the consumers because a producer's failure is what
    stopped the input the child is waiting on, and the consumers are ordered
    among themselves by :func:`_consumer_failure`.

    Parameters
    ----------
    stdin_task : asyncio.Task[object] | None
        The run's stdin writer, if it has one.
    consumers : collections.abc.Sequence[asyncio.Task[object] | None]
        The run's stream consumers in stdout-then-stderr order.
    stdin_log : _StdinWriterLog | None, default=None
        The failure the writer published before it began unwinding.

    Returns
    -------
    BaseException | None
        The highest-precedence failed task's exception, or ``None`` when no
        monitored task has failed.
    """
    failure = _writer_failure(stdin_task, stdin_log)
    return failure if failure is not None else _consumer_failure(consumers)


def _is_early_failure(
    error: BaseException,
    stdin_task: asyncio.Task[object] | None,
    consumers: cabc.Sequence[asyncio.Task[object] | None],
    *,
    stdin_log: _StdinWriterLog | None = None,
) -> bool:
    """Return whether *error* is, by identity, a monitored task's failure.

    Identity is the only reliable provenance test. A callback can raise
    :class:`TimeoutError` itself, and that type is also what an expired
    deadline produces, so a type check would misreport the callback's error as
    the run's own timeout — and, with no ``timeout=`` configured, replace it
    with an invariant failure. The rendezvous raises the very object it reads
    back off the task, or the one the writer published, so comparing identities
    is exact.

    Parameters
    ----------
    error : BaseException
        The exception a caller is about to attribute to one of the run's tasks.
    stdin_task : asyncio.Task[object] | None
        The run's stdin writer, if it has one.
    consumers : collections.abc.Sequence[asyncio.Task[object] | None]
        The run's stream consumers in stdout-then-stderr order.
    stdin_log : _StdinWriterLog | None, default=None
        The failure the writer published before it began unwinding.

    Returns
    -------
    bool
        Whether *error* is, by identity, the failure one monitored task ended
        on.
    """
    published = None if stdin_log is None else stdin_log.error
    if published is not None and published is error:
        return True
    monitored = (stdin_task, *consumers)
    return any(_stdin_writer_failure(task) is error for task in monitored)


async def _cancel_exit_wait(exit_wait: asyncio.Task[tuple[int, float]]) -> None:
    """Cancel the exit wait and complete the teardown that unwinds from it.

    Cancelling is the whole escalation. The wait's own cancellation handler
    terminates the child, waits out ``cancel_grace``, and escalates to
    ``SIGKILL``, so abandoning it here reuses the termination policy every
    other end-of-run path applies rather than restating it.

    The :class:`asyncio.CancelledError` that comes back is the expected
    outcome, not a failure: the caller is already ending the run on a failure
    it chose, and needs this helper to finish before it raises that failure.

    Parameters
    ----------
    exit_wait : asyncio.Task[tuple[int, float]]
        The child's exit wait, already scheduled.
    """
    exit_wait.cancel()
    await asyncio.gather(exit_wait, return_exceptions=True)


def _pending_tasks(
    exit_task: asyncio.Task[tuple[int, float]],
    stdin_task: asyncio.Task[object] | None,
    monitored: cabc.Sequence[asyncio.Task[object]],
) -> set[asyncio.Task[object]]:
    """Return every task the race waits on, without the absent slots."""
    return {task for task in (exit_task, stdin_task, *monitored) if task is not None}


async def _await_first_settlement(
    pending: set[asyncio.Task[object]],
    exit_task: asyncio.Task[tuple[int, float]],
) -> None:
    """Wait for one task in *pending* to settle, forwarding cancellation.

    The caller's cancellation is handed to the exit wait before it propagates:
    that wait owns the child's termination, and an abandoned wait would leave
    the child running behind the caller's teardown.

    This arm is deliberately blind to *what* was cancelled. A cancellation of
    this wait is the supervisor's own cancellation, so it must stay an
    :class:`asyncio.CancelledError` and must not be reclassified as a consumer
    failure. A consumer that ends cancelled is observed separately, after the
    wait returns, while the supervisor is still live.
    """
    try:
        await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        if not exit_task.done():
            await _shielded_cleanup(_cancel_exit_wait(exit_task))
        raise


async def _stop_on_early_failure(
    exit_task: asyncio.Task[tuple[int, float]],
    stdin_task: asyncio.Task[object] | None,
    stdin_log: _StdinWriterLog | None,
    failure: BaseException,
) -> typ.NoReturn:
    """Terminate the child, then raise the failure the run reports.

    Cancelling the exit wait *is* the termination: its cancellation handler
    terminates and reaps the child, so no termination policy is restated here.

    The writer is then asked once more, because it raises before its cleanup
    closes the pipe and so can settle only while that termination runs. Without
    that read, a run whose ``encoding`` this build does not know would be
    answered with the output decoder's ``LookupError`` rather than the
    ``StdinSourceError`` the caller's input contract promises. The consumers
    are not re-read: a failure that exists only because the run is ending is
    not a candidate for what ended it.
    """
    await _shielded_cleanup(_cancel_exit_wait(exit_task))
    settled = _writer_failure(stdin_task, stdin_log)
    raise settled if settled is not None else failure


def _unfinished(
    pending: set[asyncio.Task[object]],
    exit_task: asyncio.Task[tuple[int, float]],
) -> set[asyncio.Task[object]]:
    """Drop the tasks that settled quietly; keep the exit wait.

    This runs *after* :func:`_first_early_failure` has read the same settled
    tasks, so a consumer that ended cancelled or short of end of stream has
    already been raised as the run's outcome and never reaches here. What is
    left to drop is a producer exhausted or a stream that reached EOF: the
    ordinary way a stream ends, which must not make the next wait return at
    once. The exit wait stays whatever its state because the loop reads it
    next.

    Returns
    -------
    set[asyncio.Task[object]]
        The tasks still worth waiting on, with the exit wait always present.
    """
    return {task for task in pending if task is exit_task or not task.done()}


async def _await_exit_or_writer_failure(
    exit_wait: cabc.Coroutine[typ.Any, typ.Any, tuple[int, float]],
    stdin_task: asyncio.Task[object] | None,
    *,
    consumers: cabc.Sequence[asyncio.Task[object] | None] = (),
    stdin_log: _StdinWriterLog | None = None,
) -> tuple[int, float]:
    """Race the child's exit against its stdin writer and stream consumers.

    A task that *fails* is the run's outcome even while the child runs: the
    exit wait is cancelled, which is itself the termination, and the failure is
    raised by :func:`_stop_on_early_failure`. A consumer that ended
    *cancelled* is such an outcome too, reported as
    :class:`~cuprum._stream_consumer_errors.ConsumerCancelledError` for its
    stream: the pipe is undrained either way, so waiting for an exit that
    cannot arrive would hang the run exactly as the #551 failure did. So is a
    consumer that returned while preserving a partial capture, which
    :attr:`~cuprum._stream_drain_finish._CapturedDrain.reached_eof` marks. A
    task that merely *finishes* first changes nothing, because a producer
    exhausting its chunks or a stream reaching EOF is the ordinary way a
    stream ends.

    The precedence is fixed, so the reported failure does not depend on which
    task the loop happened to settle first: a settled exit wins, then the stdin
    writer, then the consumers in stdout-then-stderr order. A task that failed
    *after* the exit settled is left to the caller, whose post-exit await
    already reports it.

    Consumer tasks are neither cancelled nor gathered here: they belong to the
    caller, whose reconciliation settles them, and doing it here too would
    settle them twice. The supervisor's own cancellation is not one of these
    outcomes: it is forwarded to the exit wait and re-raised as a
    ``CancelledError``, so a caller still sees the cancellation it issued.

    ``exit_wait`` arrives constructed: each caller resolves the wait helper
    from its own module namespace, where the test seams replace it.

    Parameters
    ----------
    exit_wait : collections.abc.Coroutine[Any, Any, tuple[int, float]]
        The child's exit wait, not yet started.
    stdin_task : asyncio.Task[object] | None
        The run's stdin writer, or ``None`` for an inherited stdin, which has
        no pipe of cuprum's that can fail.
    consumers : collections.abc.Sequence[asyncio.Task[object] | None], default=()
        The run's stdout and stderr consumers, in that order. A stream with no
        pipe of cuprum's has ``None`` in its slot and is skipped.
    stdin_log : _StdinWriterLog | None, default=None
        The failure the writer published before it began unwinding.

    Returns
    -------
    tuple[int, float]
        The exit code and exit timestamp, as produced by the exit wait.

    Raises
    ------
    asyncio.CancelledError
        If the caller cancels the race; the exit wait is cancelled first.
    """  # ruff: ignore[docstring-extraneous-exception] - a monitored task's failure propagates.
    monitored = tuple(task for task in consumers if task is not None)
    if stdin_task is None and not monitored:
        # Nothing to race: no stream of cuprum's exists that could fail.
        return await exit_wait
    exit_task = asyncio.create_task(exit_wait)
    pending = _pending_tasks(exit_task, stdin_task, monitored)
    while True:
        await _await_first_settlement(pending, exit_task)
        if exit_task.done():
            # The exit wins outright, even over a sibling that failed in the
            # same step: its own result or exception is this call's outcome.
            return await exit_task
        # The *original* slots, not the filtered tuple: the position is what
        # labels a cancelled consumer with its stream.
        failure = _first_early_failure(stdin_task, consumers, stdin_log=stdin_log)
        if failure is not None:
            # Raised outside any ``try`` on purpose: a handler would catch
            # this very raise and re-raise only by accident of ordering.
            await _stop_on_early_failure(exit_task, stdin_task, stdin_log, failure)
        pending = _unfinished(pending, exit_task)


__all__ = [
    "_StdinWriterLog",
    "_await_consumer_outcomes",
    "_await_exit_or_writer_failure",
    "_cancel_exit_wait",
    "_consumer_failure",
    "_first_early_failure",
    "_is_early_failure",
    "_stdin_writer_failure",
]
