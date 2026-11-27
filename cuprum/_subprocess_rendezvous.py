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
is the deterministic choice instead. This module only *selects* the failure.
Settling the tasks it reports on is its callers' work, through the
reconciliation each already performs.

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
import dataclasses as dc
import typing as typ

from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._stream_consumer_errors import (
    ConsumerCancelledError,
    ConsumerStreamName,
)
from cuprum._stream_drain_finish import _CapturedDrain

if typ.TYPE_CHECKING:
    import collections.abc as cabc


# The two streams every consumer pair is ordered by. The labels are attached
# to the pair's *slots* before absent ones are filtered, because a filtered
# sequence would no longer say which stream a remaining task drains.
_CONSUMER_STREAMS: tuple[ConsumerStreamName, ...] = ("stdout", "stderr")


@dc.dataclass(slots=True)
class _StdinWriterLog:
    """The failure a stdin writer published before its own cleanup ran.

    A writer raises its failure and then unwinds, and that unwinding takes
    time: the producer's finalizer is the caller's code, run inside a window of
    its own. Asking the task during that window answers "not yet", so
    precedence would fall through to the consumer failure the writer's own
    failure caused — an unrecognized ``encoding`` fails the writer and both
    decoders, and the decoders settle first. Writing before the raise and
    reading from the race is one hand-off, not shared state.
    """

    error: BaseException | None = None

    def publish(self, error: BaseException) -> None:
        """Publish *error* as this writer's failure before it unwinds."""
        self.error = error


def _stdin_writer_failure(task: asyncio.Task[object] | None) -> BaseException | None:
    """Return one monitored task's own failure, or ``None`` when it did not fail.

    A task that is still running has not failed yet, and a cancelled one was
    torn down rather than broken. Both conditions are checked before
    :meth:`asyncio.Task.exception` is called, which would itself raise
    :class:`asyncio.CancelledError` on a cancelled task — so the order here is
    the contract, not a style choice.

    The name predates the consumers it now also answers for; every monitored
    task, stdin writer and stream reader alike, is asked the same question.

    Parameters
    ----------
    task : asyncio.Task[object] | None
        The run's stdin writer or one of its stream consumers, if it has one.

    Returns
    -------
    BaseException | None
        The exception the task ended on, or ``None`` for a task that succeeded,
        is still running, was cancelled, or does not exist.
    """
    # Written as two guards rather than one chained condition so each rejection
    # reads as the distinct contract it is: no task, versus a task whose
    # outcome is not yet or not a failure.
    if task is None:
        return None
    if not task.done():
        return None
    if task.cancelled():
        return None
    return task.exception()


def _labelled_consumers(
    consumers: cabc.Sequence[asyncio.Task[object] | None],
) -> tuple[tuple[ConsumerStreamName, asyncio.Task[object]], ...]:
    """Pair each consumer slot with its stream, dropping only absent slots.

    The pairing happens before the ``None`` slots are filtered, because the
    position in the original pair is the only thing that says which stream a
    remaining task drains.

    Returns
    -------
    tuple[tuple[ConsumerStreamName, asyncio.Task[object]], ...]
        Each present consumer with its stream name, in stream order.
    """
    return tuple(
        (stream, task)
        for stream, task in zip(_CONSUMER_STREAMS, consumers, strict=False)
        if task is not None
    )


def _consumer_outcome(
    task: asyncio.Task[object],
    stream: ConsumerStreamName,
) -> BaseException | None:
    """Return the failure one settled consumer represents, if any.

    A consumer that is still running has no outcome yet. One that ended
    *cancelled* is a failure: the cancellation leaves its pipe undrained, which
    is the #551 hang reached through a different exception. So is a consumer
    that returned while preserving a partial capture, which
    :attr:`~cuprum._stream_drain_finish._CapturedDrain.reached_eof` marks — a
    preserving drain reports its prefix instead of raising, so task state alone
    cannot see that stop. An ordinary exception is returned unchanged, so a
    callback's own ``TimeoutError`` keeps its identity. A successful
    end-of-file drain is quiet.

    A cancelled consumer is reported through
    :class:`~cuprum._stream_consumer_errors.ConsumerCancelledError` rather than
    by re-raising its ``CancelledError``: that type is indistinguishable from
    the caller's own cancellation.

    Parameters
    ----------
    task : asyncio.Task[object]
        One of the run's stream consumers.
    stream : ConsumerStreamName
        The stream whose pipe that consumer drains.

    Returns
    -------
    BaseException | None
        The failure this consumer's outcome represents, or ``None`` when the
        consumer is pending or finished quietly.
    """
    if not task.done():
        return None
    if task.cancelled():
        return _cancelled_consumer_error(task, stream)
    failure = task.exception()
    if failure is not None:
        return failure
    result = task.result()
    if isinstance(result, _CapturedDrain) and not result.reached_eof:
        return ConsumerCancelledError(stream)
    return None


def _cancelled_consumer_error(
    task: asyncio.Task[object],
    stream: ConsumerStreamName,
) -> ConsumerCancelledError:
    """Build the error for a cancelled consumer, chaining its cancellation.

    :meth:`asyncio.Task.exception` raises the ``CancelledError`` a cancelled
    task carries, so the retrieval happens in an ``except`` arm. The error is
    built whether or not that retrieval yields an object, which keeps the
    classification independent of the interpreter's cancellation bookkeeping.

    Returns
    -------
    ConsumerCancelledError
        The stream-labelled error, with the task's own cancellation chained as
        its cause when one is available.
    """
    error = ConsumerCancelledError(stream)
    try:
        task.exception()
    except asyncio.CancelledError as cancelled:
        error.__cause__ = cancelled
    return error


def _consumer_failure(
    consumers: cabc.Sequence[asyncio.Task[object] | None],
) -> BaseException | None:
    """Return the first failed consumer's outcome, in stream order.

    stdout is checked before stderr so a run with two failing readers reports
    the same one on every attempt. ``asyncio.wait`` reports a *set* of settled
    tasks, which carries no completion order, so the pair's own order is the
    only deterministic tie-break available.

    Parameters
    ----------
    consumers : collections.abc.Sequence[asyncio.Task[object] | None]
        The run's stream consumers in stream order. A slot cuprum holds no pipe
        for is ``None`` and is skipped.

    Returns
    -------
    BaseException | None
        The first consumer outcome that counts as a failure, or ``None`` when
        none has failed.
    """
    for stream, task in _labelled_consumers(consumers):
        failure = _consumer_outcome(task, stream)
        if failure is not None:
            return failure
    return None


def _first_completion_failure(
    finished: cabc.Sequence[asyncio.Task[object]],
    processed: int,
    streams: cabc.Mapping[asyncio.Task[object], ConsumerStreamName],
) -> tuple[BaseException | None, int]:
    """Classify the completions recorded after *processed*, in order.

    Returns
    -------
    tuple[BaseException | None, int]
        The first failure found and the new processed count, or ``(None, n)``
        when every remaining completion was quiet.
    """
    while processed < len(finished):
        task = finished[processed]
        processed += 1
        failure = _consumer_outcome(task, streams[task])
        if failure is not None:
            return failure, processed
    return None, processed


async def _await_consumer_outcomes(
    consumers: cabc.Sequence[asyncio.Task[object] | None],
    *,
    budget: float | None = None,
) -> BaseException | None:
    """Wait until every consumer settles, returning the first failure seen.

    Completions are classified in the order they arrive, so two consumers that
    settle in one loop step are attributed by completion order rather than by
    task hashing. The first failure stops the wait: waiting for a wedged
    sibling would spend the whole budget and report it as the run's deadline
    instead of the failure that ended the run. A consumer that ended cancelled
    or returned a partial capture is a failure for its stream, reported through
    :class:`~cuprum._stream_consumer_errors.ConsumerCancelledError`.

    Both single-command supervisors use this wait after the exit. They differ
    only in whether a budget applies: ``lines()`` has whatever is left of the
    run's deadline, while the streamed run's consumers are bounded by the
    enclosing deadline scope.

    Parameters
    ----------
    consumers : collections.abc.Sequence[asyncio.Task[object] | None]
        The run's stream consumers in stdout-then-stderr order. A slot cuprum
        holds no pipe for is ``None`` and is skipped.
    budget : float | None, default=None
        The budget for the whole wait, or ``None`` to wait without one.

    Returns
    -------
    BaseException | None
        The first consumer failure, in completion order, or ``None`` when every
        consumer settled quietly.

    Raises
    ------
    TimeoutError
        When *budget* elapses with a consumer still pending. A budget already
        spent when the wait began is reported the same way, because
        ``asyncio.wait`` returns at once for a non-positive timeout and leaves
        every unsettled consumer in *pending*.
    """
    streams = {task: stream for stream, task in _labelled_consumers(consumers)}
    if not streams:
        return None
    finished: list[asyncio.Task[object]] = []
    for task in streams:
        task.add_done_callback(finished.append)
    pending = set(streams)
    processed = 0
    loop = asyncio.get_running_loop()
    # One absolute budget rather than a fresh remainder per iteration: a loop
    # that re-measured the caller's remainder would let each round restart the
    # deadline it is meant to share.
    deadline = None if budget is None else loop.time() + budget
    while pending:
        done, _ = await asyncio.wait(
            pending,
            timeout=None if deadline is None else max(0.0, deadline - loop.time()),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            # The wait returned without a completion, so its only bound — the
            # remaining budget — is spent.
            raise TimeoutError
        # Done callbacks are registered ahead of the wait's own release, so
        # this round's completions are already recorded; the yield states that
        # ordering explicitly rather than relying on it as an asyncio detail.
        await asyncio.sleep(0)
        failure, processed = _first_completion_failure(finished, processed, streams)
        if failure is not None:
            return failure
        pending = {task for task in pending if not task.done()}
    return None


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
    "_consumer_outcome",
    "_first_completion_failure",
    "_first_early_failure",
    "_is_early_failure",
    "_labelled_consumers",
    "_stdin_writer_failure",
]
