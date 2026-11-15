"""Racing a child's exit against the tasks that feed or drain it.

Ending a run divides in three, and this module owns the middle: the child's own
deadline is ``cuprum._subprocess_deadline``'s, and the parent's task
reconciliation is ``cuprum._subprocess_wait``'s, but the decision to end a run
*because one of its stream tasks died* belongs to neither. Neither the child
nor the parent's task set is at fault, so neither module is the right place to
notice it.

The arithmetic is the reason the race exists at all. Awaiting the child first
looks correct, and is, as long as a producer that fails also stops the child.
It does not: the writer's own teardown closes the pipe, so a child that reads
to EOF and then works — or one that never reads at all — keeps running after
the failure and the caller is told the child was slow when its input source was
already dead. That is not a hypothetical shape; it is what a `head`-like child
that ignores its input, or any child with real work after its last read,
produces. A stream *consumer* fails the same way from the other end: a raising
``on_line`` callback ends the task draining the child's output, nothing reads
the pipe any more, and a child that keeps writing blocks on a full pipe rather
than exiting — so the exit the coordinator is waiting for never arrives.

The resolution is deliberately narrow. A writer or consumer that *finishes*
first is the ordinary case — a producer exhausted, a stream closed, the child
still draining — and the run continues to the child's exit as it always did.
Only a task that *failed* is an outcome, and the exit wait is then cancelled,
which is itself the termination: ``_wait_for_exit_code``'s cancellation handler
escalates through ``_terminate_all_shielded``. No termination policy is
restated here.

The precedence between the early-failure sources is state-based and fixed, so a
run with several failing tasks reports the same one every time: a settled exit
wins outright, then the stdin writer, then the stream consumers in
stdout-then-stderr order. The consumer that failed first is *not* tracked,
because ``asyncio.wait`` reports a set rather than an order; the stream order
is the deterministic choice instead. This module only *selects* the failure.
Settling the tasks it reports on is its callers' work, through the
reconciliation each already performs.

That precedence is read twice, not once, and the second read is what keeps a
source failure from hiding behind the failures it caused. A stdin writer raises
its own exception before its cleanup releases the pipe, so while a consumer has
already failed the writer can still be running; terminating the child is what
lets it finish. So the race stops on the failure it can see, cancels the exit
wait, and only then reads the precedence again over everything that settled.

A second read alone is not enough, because the window has two ends. A writer
that has already raised is *still running* while it unwinds — its producer's
finalizer is the caller's code and is given a window of its own — so a read
taken during that unwinding sees a task that has not failed. The writer
therefore publishes its failure before it starts unwinding, and precedence
reads that publication before the task.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum._process_lifecycle import _shielded_cleanup

if typ.TYPE_CHECKING:
    import collections.abc as cabc


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


def _consumer_failure(
    consumers: cabc.Sequence[asyncio.Task[object] | None],
) -> BaseException | None:
    """Return the first failed consumer's exception, in stream order.

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
        The first consumer's own failure, or ``None`` when none has failed.
    """
    for task in consumers:
        failure = _stdin_writer_failure(task)
        if failure is not None:
            return failure
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


async def _await_exit_or_writer_failure(
    exit_wait: cabc.Coroutine[typ.Any, typ.Any, tuple[int, float]],
    stdin_task: asyncio.Task[object] | None,
    *,
    consumers: cabc.Sequence[asyncio.Task[object] | None] = (),
    stdin_log: _StdinWriterLog | None = None,
) -> tuple[int, float]:
    """Race the child's exit against its stdin writer and stream consumers.

    A producer or consumer that fails is the run's outcome even while the child
    is still running, and the exit wait is cancelled — which is what terminates
    the child — before that failure is raised. A task that merely *finishes*
    first changes nothing: the run still ends on the child's exit, because a
    producer exhausting its chunks or a stream reaching EOF is the ordinary way
    a stream ends.

    The precedence is fixed so the reported failure does not depend on which
    task the event loop happened to settle first: a settled exit wins, then the
    stdin writer, then the consumers in stdout-then-stderr order. A task that
    failed *after* the exit settled is likewise left to the caller, whose
    existing post-exit await already reports it; racing to catch that case too
    would only duplicate a check the caller must make anyway.

    The precedence is applied once to decide that the run must stop and once
    more after the exit wait's cancellation has terminated the child, because a
    stdin writer unwinding its own cleanup can settle on its failure only
    during that termination. Without the second read, a run whose ``encoding``
    this build does not know would report its output decoder's
    :class:`LookupError` instead of the :class:`~cuprum.sh.StdinSourceError` the
    caller's input contract promises.

    *stdin_log* closes the other end of the same window. A writer that has
    already raised is still running while it unwinds, so a read taken then sees
    no failure on the task at all; the writer publishes its failure to the log
    first, and both reads consult it before the task.

    Consumer tasks are *not* cancelled or gathered here. They belong to the
    caller, whose reconciliation on every failure route settles them; doing it
    here as well would settle them twice and race the caller's own teardown.

    ``exit_wait`` is passed in already constructed rather than created here so
    each caller keeps resolving the wait helper from its own module namespace.
    That is where the test suite's monkeypatch seams replace it, so a call that
    reached for the name in this module would silently bypass them.

    Parameters
    ----------
    exit_wait : collections.abc.Coroutine[Any, Any, tuple[int, float]]
        The child's exit wait, not yet started.
    stdin_task : asyncio.Task[object] | None
        The run's stdin writer, if it has one. ``None`` for an inherited stdin,
        which has no pipe of cuprum's and so nothing that can fail.
    consumers : collections.abc.Sequence[asyncio.Task[object] | None], default=()
        The run's stdout and stderr stream consumers, in that order. A stream
        cuprum holds no pipe for has ``None`` in its slot and is skipped.
    stdin_log : _StdinWriterLog | None, default=None
        The failure the writer published before it began unwinding. Absent for
        a run whose writer has no such hand-off, which is then read off the
        task alone.

    Returns
    -------
    tuple[int, float]
        The exit code and exit timestamp, as produced by the exit wait.

    Raises
    ------
    asyncio.CancelledError
        If the caller cancels the race. The exit wait owns the child's
        termination, so the cancellation is forwarded to it before this
        re-raises.
    """  # ruff: ignore[docstring-extraneous-exception] - a monitored task's failure propagates.
    monitored = tuple(task for task in consumers if task is not None)
    if stdin_task is None and not monitored:
        # Nothing to race: an inherited stdin and unobserved streams have
        # nothing of cuprum's that can fail.
        return await exit_wait
    exit_task = asyncio.create_task(exit_wait)
    pending = {task for task in (exit_task, stdin_task, *monitored) if task is not None}
    while True:
        try:
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            # The caller was cancelled while the tasks ran side by side. Handing
            # the cancellation to the exit wait is what terminates the child;
            # without it the wait would be abandoned mid-flight and the child
            # left running behind the caller's teardown.
            if not exit_task.done():
                await _shielded_cleanup(_cancel_exit_wait(exit_task))
            raise
        if exit_task.done():
            # The exit wins outright, including over a sibling that failed in
            # the same step: its result or its own exception is this call's
            # outcome, and any consumer failure is the caller's to report.
            return await exit_task
        failure = _first_early_failure(stdin_task, monitored, stdin_log=stdin_log)
        if failure is not None:
            # Raised outside the ``try`` above on purpose: inside it, the
            # handler would catch this very raise, see a settled exit task, and
            # re-raise — correct, but only by accident of ordering.
            await _shielded_cleanup(_cancel_exit_wait(exit_task))
            # The precedence is read again now that the teardown has run, and
            # that second read is load-bearing rather than defensive. A stdin
            # writer raises its own failure before its cleanup closes the pipe,
            # so it can still be *running* while a consumer has already failed:
            # an unrecognized ``encoding`` fails the writer and both decoders,
            # and the decoders fail first. Terminating the child is what lets
            # the writer finish, so the source failure the caller's data
            # contract names is only visible here. Reporting the consumer's
            # error instead would answer a bad stdin encoding with the reader
            # failure it caused.
            settled = _first_early_failure(
                stdin_task,
                monitored,
                stdin_log=stdin_log,
            )
            raise settled if settled is not None else failure
        # Every task that settled did so without failing: drop the finished
        # ones so the next wait does not return on them at once, then keep
        # waiting for the exit or a failure.
        pending = {task for task in pending if task is exit_task or not task.done()}


__all__ = [
    "_StdinWriterLog",
    "_await_exit_or_writer_failure",
    "_cancel_exit_wait",
    "_consumer_failure",
    "_first_early_failure",
    "_is_early_failure",
    "_stdin_writer_failure",
]
