"""Racing a child's exit against the stdin writer that feeds it.

Ending a run divides in three, and this module owns the middle: the child's own
deadline is ``cuprum._subprocess_deadline``'s, and the parent's task
reconciliation is ``cuprum._subprocess_wait``'s, but the decision to end a run
*because its input source died* belongs to neither. Neither the child nor the
parent's task set is at fault, so neither module is the right place to notice
it.

The arithmetic is the reason the race exists at all. Awaiting the child first
looks correct, and is, as long as a producer that fails also stops the child.
It does not: the writer's own teardown closes the pipe, so a child that reads
to EOF and then works — or one that never reads at all — keeps running after
the failure and the caller is told the child was slow when its input source was
already dead. That is not a hypothetical shape; it is what a `head`-like child
that ignores its input, or any child with real work after its last read,
produces.

The resolution is deliberately narrow. A writer that *finishes* first is the
ordinary case — a producer exhausted, the pipe closed, the child still
draining — and the run continues to the child's exit as it always did. Only a
writer that *failed* is an outcome, and the exit wait is then cancelled, which
is itself the termination: ``_wait_for_exit_code``'s cancellation handler
escalates through ``_terminate_all_shielded``. No termination policy is
restated here.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._process_lifecycle import _shielded_cleanup

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _stdin_writer_failure(task: asyncio.Task[None] | None) -> BaseException | None:
    """Return the stdin writer's own failure, or ``None`` when it did not fail.

    A writer that is still running has not failed yet, and a cancelled one was
    torn down rather than broken. Both conditions are checked before
    :meth:`asyncio.Task.exception` is called, which would itself raise
    :class:`asyncio.CancelledError` on a cancelled task — so the order here is
    the contract, not a style choice.

    Parameters
    ----------
    task : asyncio.Task[None] | None
        The run's stdin writer, if it has one.

    Returns
    -------
    BaseException | None
        The exception the writer ended on, or ``None`` for a writer that
        succeeded, is still running, was cancelled, or does not exist.
    """
    # Written as two guards rather than one chained condition so each rejection
    # reads as the distinct contract it is: no writer, versus a writer whose
    # outcome is not yet or not a failure.
    if task is None:
        return None
    if not task.done():
        return None
    if task.cancelled():
        return None
    return task.exception()


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
    stdin_task: asyncio.Task[None] | None,
) -> tuple[int, float]:
    """Race the child's exit against the stdin writer, ending on either.

    A producer that fails is the run's outcome even while the child is still
    running, and the exit wait is cancelled — which is what terminates the
    child — before that failure is raised. A producer that merely finishes
    first changes nothing: the run still ends on the child's exit, because a
    producer exhausting its chunks is the ordinary way a stream ends.

    A writer that failed *after* the exit settled is left to the caller, whose
    existing post-exit await already reports it; racing to catch that case too
    would only duplicate a check the caller must make anyway.

    ``exit_wait`` is passed in already constructed rather than created here so
    each caller keeps resolving the wait helper from its own module namespace.
    That is where the test suite's monkeypatch seams replace it, so a call that
    reached for the name in this module would silently bypass them.

    Parameters
    ----------
    exit_wait : collections.abc.Coroutine[Any, Any, tuple[int, float]]
        The child's exit wait, not yet started.
    stdin_task : asyncio.Task[None] | None
        The run's stdin writer, if it has one. ``None`` for an inherited stdin,
        which has no pipe of cuprum's and so nothing that can fail.

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
    """  # ruff: ignore[docstring-extraneous-exception] - the writer's StdinSourceError propagates.
    if stdin_task is None:
        # No writer to race: an inherited stdin has nothing that can fail.
        return await exit_wait
    exit_task = asyncio.create_task(exit_wait)
    try:
        await asyncio.wait(
            (exit_task, stdin_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
    except BaseException:
        # The caller was cancelled while the two ran side by side. Handing the
        # cancellation to the exit wait is what terminates the child; without
        # it the wait would be abandoned mid-flight and the child left running
        # behind the caller's teardown.
        if not exit_task.done():
            await _shielded_cleanup(_cancel_exit_wait(exit_task))
        raise
    if not exit_task.done():
        failure = _stdin_writer_failure(stdin_task)
        if failure is not None:
            # Raised outside the ``try`` above on purpose: inside it, the
            # handler would catch this very raise, see a settled exit task, and
            # re-raise — correct, but only by accident of ordering.
            await _shielded_cleanup(_cancel_exit_wait(exit_task))
            raise failure
    return await exit_task


__all__ = [
    "_await_exit_or_writer_failure",
    "_cancel_exit_wait",
    "_stdin_writer_failure",
]
