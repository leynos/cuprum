"""Reclaiming a stdin writer: how it is stopped and how long that is given.

Split from ``cuprum._subprocess_stdin`` when the collected policy pushed that
module past the 400-line ceiling. The seam is the one the policy already drew:
that module owns *what* cuprum writes into the child's pipe and how the pipe
itself is released, while this one owns *how a writer that is still running is
taken back* once the run has no further use for it.

Two kinds of caller reach this policy, and they reach it for the same reason.
An ordinary run settles its writer once the child has exited
(``cuprum._subprocess_stream_run`` and ``cuprum._line_stream.drain``); a
teardown cancels it (``cuprum._subprocess_wait`` and
``cuprum._subprocess_stream_run``); and a producer's own finalization reclaims
whatever is still parked after its bounded window
(``cuprum._subprocess_stdin_finalize``). Keeping the window and both operations
in one module is what makes them agree: every path that bounds a writer's exit
bounds it with the same figure, and a change to that figure cannot leave one
path behind.
"""

from __future__ import annotations

import asyncio

from cuprum._process_lifecycle import _shielded_cleanup

# How long a stdin writer is given to notice it should stop — both when the
# child it was feeding has exited and when the writer is being cancelled. Fixed
# and short for the same reason the readers' EOF grace is (see
# ``_CAPTURE_EOF_GRACE_S`` in ``cuprum._subprocess_wait``): the bytes are
# already written and the child that would read them is gone, so the only thing
# still being waited for is a pipe end held open by the child's own
# descendants. A writer that can finish at all finishes in well under this; one
# that cannot will never finish, however long it is given.
#
# It is the unit every other budget below is counted in, because a cleanup
# *step* is exactly one bounded wait of this length. Budgets are declared as
# whole numbers of steps and multiplied out rather than written as literals,
# for the reason the nesting below makes concrete: cuprum's cleanup runs
# inside the very task cuprum is reclaiming, so a reclaim window that does not
# account for the nested steps expires while the cleanup it is waiting for is
# still running — and then reports the reclaim as done. Deriving the outer
# figure from the inner ones is what keeps the two in step; a hand-written
# total would have to be re-derived by hand every time a step is added.
_STDIN_SETTLE_GRACE_S = 0.25

# Finalizing a producer is a two-step sequence, not one wait: the window the
# finalizer is given to finish on its own, and then a reclaim in which a
# finalizer that is still suspended is cancelled and awaited. A finalizer
# interrupted partway through is exactly the case that needs the second step —
# it has been asked to stop and has not stopped yet.
_STDIN_FINALIZE_STEPS = 2

# A writer's teardown releases the pipe and *then* finalizes whatever producer
# it was pulling from, so it is that whole finalization plus the pipe-release
# wait of its own. This is the nesting CodeRabbit identified: a writer given
# only one step is cancelled, spends its own three, and outlives the window
# that was supposed to contain it.
_STDIN_WRITER_TEARDOWN_STEPS = 1 + _STDIN_FINALIZE_STEPS

# What a finalizer *task* is allowed when it is reclaimed: one step, on its
# own. It is deliberately not the writer budget — a finalizer task runs caller
# code and nothing of cuprum's, so it has no nested steps to account for, and
# giving it the writer's figure would re-open the same nesting one level down.
_STDIN_FINALIZER_RECLAIM_S = _STDIN_SETTLE_GRACE_S

# What cancelling a *writer* is allowed. It must cover the writer's whole
# teardown, because the writer runs that teardown inside its own cancellation:
# `_close_stdin` bounded by one step, then `_finalize_stdin_source` bounded by
# `_STDIN_FINALIZE_STEPS`. A cooperative writer — one that needs the pipe
# released and a suspended finalizer cancelled, but never ignores the
# cancellation it is given — therefore settles inside this window, which is
# the guarantee a cancelled run depends on before it reports that its tasks
# are gone.
_STDIN_RECLAIM_BUDGET_S = _STDIN_SETTLE_GRACE_S * _STDIN_WRITER_TEARDOWN_STEPS


async def _cancel_stdin_writer(
    stdin_task: asyncio.Task[None] | None,
    *,
    budget: float = _STDIN_RECLAIM_BUDGET_S,
) -> None:
    """Cancel and drain a task, tolerating any raised error.

    Used on the timeout and cancellation paths to reclaim a writer that may be
    blocked draining bytes into an unread pipe, so its cleanup cannot delay the
    surrounding failure work.

    Draining it is bounded: a task can absorb cancellation in its own
    ``finally`` and go on blocking there, which is exactly what the writer's
    pipe close used to do. Reclaiming a writer is meant to *bound* the wait for
    it, so an unbounded drain here would hand that guarantee back to the thing
    it was written to contain.

    ``budget`` defaults to the writer's whole teardown because a *writer* is
    what this was written for, and a writer's cancellation runs its own nested
    cleanup: a pipe release and a producer finalization. Reclaiming anything
    else — a bare finalizer task, which nests nothing — passes its own smaller
    figure rather than inheriting a window sized for work it will never do.

    Parameters
    ----------
    stdin_task : asyncio.Task[None] | None
        The task to cancel and drain. ``None`` is a no-op, so a caller need not
        test for the inherited-stdin case before calling.
    budget : float
        How long the task is given to finish its own cleanup after being
        cancelled.
    """
    if stdin_task is None:
        return
    stdin_task.cancel()
    await asyncio.wait((stdin_task,), timeout=budget)


async def _settle_stdin_writer(stdin_task: asyncio.Task[None] | None) -> None:
    """Reclaim the stdin writer once the child it was feeding has exited.

    The exit path is the one place a writer can outlive what it writes to. A
    timeout or a cancellation reclaims the writer through
    :func:`_cancel_stdin_writer`, and an ordinary run has the producer exhaust
    and the writer close the pipe *before* the child is done — so by the time
    an exit settles, a writer that is still pending is one whose remaining work
    can reach nobody: the child is reaped and its end of the pipe is gone.

    Awaiting such a writer unconditionally is what this replaces. A producer
    parked in ``anext`` has no continuation that would ever end the wait, so an
    unbounded await made a stalled producer indistinguishable from a stalled
    child and outlasted the run's own deadline: the terminator fired, the child
    was killed, and the caller kept waiting on the producer.

    A writer that already finished is awaited, so a failure it raised before
    the exit settled still reaches the caller. One that is still pending is
    given the settle window, which is what lets a write already in flight fail
    with ``EPIPE`` and take the early-close path, and is then cancelled and
    reclaimed exactly as the teardown paths reclaim it. A producer that would
    have failed after that window loses the report, which is the same trade
    every other teardown path already makes; the alternative is a run that
    never ends.

    The wait itself can be broken. This step runs *after* the child has exited,
    so the cancellation guard that covered the exit wait is already behind it,
    and a caller cancelling now lands on the wait below. A broken wait returns
    without the timeout having fired, so the writer is neither finished nor due
    for the reclaim this helper would normally perform — it is owed exactly the
    same cancellation the expiry path gives it. Raising is right: cancellation
    is the caller's control flow and must propagate. Raising *without*
    reclaiming is not, because it leaves the writer and its producer pending on
    the loop behind the cancelled run.

    Parameters
    ----------
    stdin_task : asyncio.Task[None] | None
        The run's stdin writer. ``None`` for an inherited stdin, which has no
        pipe of cuprum's and so nothing to reclaim.

    Raises
    ------
    BaseException
        Whatever the writer raised, when it failed before or within the
        window. A writer this helper cancels reports nothing: it was reclaimed,
        not broken.
    asyncio.CancelledError
        If the settle window is broken by cancellation. The writer is
        reclaimed first, so the cancellation cannot outrun the work it is
        ending.
    """  # ruff: ignore[docstring-extraneous-exception] - the writer's own exception propagates.
    if stdin_task is None:
        return
    if stdin_task.done():
        # Nothing to reclaim, and the failure it already carries is the
        # caller's to see.
        await stdin_task
        return
    # The window *and* the reclaim are shielded together, not merely the wait.
    # ``_shielded_cleanup`` re-raises the caller's cancellation once its
    # coroutine completes, so a shielded wait alone would propagate straight
    # past the code below and leave the writer pending anyway — just later.
    # Shielding the whole settle is what makes the reclaim reachable on the
    # cancellation path as well as the expiry one.
    await _shielded_cleanup(_expire_or_reclaim_writer(stdin_task))


async def _expire_or_reclaim_writer(stdin_task: asyncio.Task[None]) -> None:
    """Give a parked writer the settle window, then reclaim it if it is there.

    Split from :func:`_settle_stdin_writer` so the shield wraps one coroutine
    rather than a sequence of awaits: shielding each step separately would let
    a cancellation land *between* them, which is the same unreachable-reclaim
    defect at a finer grain.
    """
    await asyncio.wait((stdin_task,), timeout=_STDIN_SETTLE_GRACE_S)
    if stdin_task.done():
        await stdin_task
        return
    await _cancel_stdin_writer(stdin_task)


__all__ = [
    "_STDIN_FINALIZER_RECLAIM_S",
    "_STDIN_FINALIZE_STEPS",
    "_STDIN_RECLAIM_BUDGET_S",
    "_STDIN_SETTLE_GRACE_S",
    "_STDIN_WRITER_TEARDOWN_STEPS",
    "_cancel_stdin_writer",
    "_settle_stdin_writer",
]
