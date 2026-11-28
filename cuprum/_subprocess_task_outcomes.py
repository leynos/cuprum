"""Reading a monitored task's outcome, and waiting on its siblings.

A single-command supervisor learns that a pipe is undrained in two places: the
exit race in ``cuprum._subprocess_rendezvous`` and the post-exit wait that
follows a settled exit. Both need the same answer to "what did this task's
outcome mean?", so the answer lives here once.

A writer raises its failure and then unwinds, and that unwinding takes time:
the producer's finalizer is the caller's code. Asking the task during that
window answers "not yet", so the writer publishes its failure to
``_StdinWriterLog`` before the raise. Both writer reads consult the publication
ahead of the task. The second read, after termination, stays in the rendezvous
because the termination it depends on belongs there.

An ordinary exception is a failure and is returned unchanged, so a callback's
own ``TimeoutError`` keeps the identity provenance relies on. A task that
ended *cancelled* is a failure too: its pipe is undrained, so the child blocks
on a full buffer and the exit never arrives. A task that returned while
preserving a partial capture is the same failure spelled differently;
``_CapturedDrain.reached_eof`` marks it. Both are reported through
``ConsumerCancelledError`` naming the stream, which a caller can distinguish
from its own cancellation. A pending task and an end-of-file drain are quiet.

The fixed stream order is the deterministic tie-break for a settled set, and
``_await_consumer_outcomes`` records completion order through done callbacks
for the post-exit wait. It stops on the first failure rather than spending a
budget on a wedged sibling.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

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
    if task is None:
        return None
    # One settled-and-unabandoned test, evaluated before ``exception``: asking
    # a cancelled task raises, so the cancellation has to be excluded first.
    settled = task.done() and not task.cancelled()
    return task.exception() if settled else None


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
    processed: set[asyncio.Task[object]],
    streams: cabc.Mapping[asyncio.Task[object], ConsumerStreamName],
) -> BaseException | None:
    """Classify recorded completions that have not been classified yet.

    *finished* is the completion order recorded by the done callbacks, so the
    first unprocessed entry is the consumer that finished first.

    Returns
    -------
    BaseException | None
        The first failure among the new completions, or ``None`` when every one
        was quiet.
    """
    for task in finished:
        if task in processed:
            continue
        processed.add(task)
        failure = _consumer_outcome(task, streams[task])
        if failure is not None:
            return failure
    return None


def _classify_unrecorded_settled(
    streams: cabc.Mapping[asyncio.Task[object], ConsumerStreamName],
    processed: set[asyncio.Task[object]],
) -> BaseException | None:
    """Classify settled consumers whose done callback has not run yet.

    A consumer can settle after the wait computed its completion set but
    before the callback that records completion order runs. Dropping it as
    done without classification would let a raw cancellation or a partial
    capture pass as a quiet finish. Stream order is the deterministic
    tie-break when the completion order is no longer recoverable.

    Returns
    -------
    BaseException | None
        The first failure among the newly settled consumers, or ``None`` when
        every one was quiet.
    """
    for task, stream in streams.items():
        if task in processed or not task.done():
            continue
        processed.add(task)
        failure = _consumer_outcome(task, stream)
        if failure is not None:
            return failure
    return None


async def _wait_for_consumer_completion(
    pending: set[asyncio.Task[object]],
    deadline: float | None,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Wait for at least one pending consumer to settle.

    The done callbacks are registered ahead of the wait's own release, so this
    round's completions are already recorded when the wait returns; the yield
    states that ordering explicitly rather than relying on it as an asyncio
    detail.

    Raises
    ------
    TimeoutError
        When the budget is spent before a consumer settles.
    """
    done, _ = await asyncio.wait(
        pending,
        timeout=None if deadline is None else max(0.0, deadline - loop.time()),
        return_when=asyncio.FIRST_COMPLETED,
    )
    if not done:
        # The wait returned without a completion, so its only bound — the
        # remaining budget — is spent.
        raise TimeoutError
    await asyncio.sleep(0)


async def _await_consumer_outcomes(
    consumers: cabc.Sequence[asyncio.Task[object] | None],
    *,
    budget: float | None = None,
) -> BaseException | None:
    """Wait until every consumer settles, returning the first failure seen.

    Completions are classified in the order they arrive, so two consumers that
    settle in one loop step are attributed by completion order rather than by
    task hashing. When several consumers were already settled before the wait
    began, the order their done callbacks were registered in — stdout before
    stderr — is the deterministic tie-break. Every settled consumer is
    classified before it is dropped, including one whose done callback has not
    run yet; otherwise a raw cancellation or a partial capture could pass as a
    quiet finish and surface later through a gather.

    The first failure stops the wait: waiting for a wedged sibling would spend
    the whole budget and report it as the run's deadline instead of the failure
    that ended the run. A consumer that ended cancelled or returned a partial
    capture is a failure for its stream, reported through
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
    """  # ruff: ignore[docstring-extraneous-exception] - the helper raises the expiry.
    streams = {task: stream for stream, task in _labelled_consumers(consumers)}
    if not streams:
        return None
    finished: list[asyncio.Task[object]] = []
    for task in streams:
        task.add_done_callback(finished.append)
    pending = set(streams)
    processed: set[asyncio.Task[object]] = set()
    loop = asyncio.get_running_loop()
    # One absolute budget rather than a fresh remainder per iteration: a loop
    # that re-measured the caller's remainder would let each round restart the
    # deadline it is meant to share.
    deadline = None if budget is None else loop.time() + budget
    while pending:
        await _wait_for_consumer_completion(pending, deadline, loop)
        failure = _first_completion_failure(finished, processed, streams)
        if failure is not None:
            return failure
        failure = _classify_unrecorded_settled(streams, processed)
        if failure is not None:
            return failure
        pending = {task for task in pending if not task.done()}
    return None


__all__ = [
    "_StdinWriterLog",
    "_await_consumer_outcomes",
    "_classify_unrecorded_settled",
    "_consumer_failure",
    "_consumer_outcome",
    "_first_completion_failure",
    "_labelled_consumers",
    "_stdin_writer_failure",
    "_wait_for_consumer_completion",
]
