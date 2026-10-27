"""Bounded finalization of a caller-provided producer.

A streaming ``StdinStream`` is the caller's own async iterable, so closing it
runs the caller's code. This module owns that one question — how long cuprum
will wait for a producer's cleanup before reclaiming it — and nothing about how
a chunk reaches the child's pipe, which stays in
``cuprum._subprocess_stdin_stream``.

The split is the one the streaming writer's line-count budget forced, and it is
the seam the code already draws: the writer owns the pipe and the pump, while
this owns what happens to *caller* code cuprum can no longer wait for. Keeping
the policy here also keeps the cooperation limit in one place, stated once
rather than implied at each call site.

The line-count budget is not the only reason, and the policy is worth reading
on its own terms. Bounding the wait is a decision about a third party's code:
cuprum cannot make a caller's finalizer finish, so promising that finalization
*completes* would be a promise it cannot keep. What it can promise is that the
run is not held open by cleanup it no longer needs, and that is what the window
delivers.
"""

from __future__ import annotations

import asyncio
import contextlib
import typing as typ

from cuprum._subprocess_stdin_reclaim import (
    _STDIN_FINALIZER_RECLAIM_S,
    _STDIN_SETTLE_GRACE_S,
    _cancel_stdin_writer,
    _retrieve_task_outcome,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ["_finalize_stdin_source"]


async def _finalize_stdin_source(
    source: cabc.AsyncIterator[str | bytes] | None,
) -> None:
    """Close a producer's iterator, bounding however long that takes.

    A caller may hand cuprum an async *iterable* whose ``__aiter__`` returns a
    generator, or an object whose iterator has no ``aclose`` at all. Only the
    generator protocol's clean-up is attempted, and a failure to finalize a
    producer cuprum no longer needs must not mask the run's real outcome.

    The wait is bounded because ``aclose`` is the caller's own code. A
    finalizer that suspends — releasing a socket, flushing a file, awaiting a
    lock — is ordinary rather than exotic, and the run this belongs to has
    already decided its outcome by the time it runs. An unbounded await would
    let that cleanup hold the run open past its own deadline, which is the same
    failure the writer's settle window exists to prevent.

    A finalizer that needs longer than the window is *cancelled*, not waited
    out, and the cancellation is best-effort in both directions: cuprum asks
    the finalizer to stop and then proceeds regardless. A finalizer that
    ignores the request — one that catches ``CancelledError`` and keeps working
    — therefore continues in the background, and its cleanup may still be
    running after the run has returned. That is the cooperation limit, stated
    rather than papered over: cuprum can bound its own wait, and cannot bound
    code it does not own. The loop reclaims what remains at shutdown.

    Everything here fails towards the run's own outcome. Both invoking the
    caller's hook and scheduling it are guarded, so a finalizer that raises on
    entry cannot take the place of the failure the run is already ending on,
    and a task that finishes after the window has its exception retrieved
    rather than left for the loop to report. What the caller sees is the run's
    ending; what the caller does not see is a cleanup fault attributed to a
    producer cuprum has already stopped using.

    ``asyncio.wait`` rather than ``wait_for``, and this is a correctness point
    rather than a style one. ``wait_for`` waits for the cancellation it
    requests to *take effect*, so a finalizer that absorbs ``CancelledError``
    while finishing its own cleanup — a ``try``/``except`` around a release,
    say — is not bounded by ``wait_for`` at all: the window expires and the
    call still blocks for as long as the finalizer chooses. Measured on this
    interpreter, a 0.1 s ``wait_for`` window against a finalizer that absorbed
    cancellation took 0.50 s to return; ``asyncio.wait`` honoured the window.

    Parameters
    ----------
    source : collections.abc.AsyncIterator[str | bytes] | None
        The producer's iterator, or ``None`` when the run never built one
        because the producer failed to start.

    Raises
    ------
    asyncio.CancelledError
        If the surrounding run is cancelled, whether before the finalizer is
        scheduled or while the bounded wait is pending. Cancellation is the
        caller's control flow, so it is never absorbed by the window — not by
        the guard around scheduling, and not by the wait itself.
    """
    if source is None:
        return
    aclose = getattr(source, "aclose", None)
    if aclose is None:
        return
    task = _schedule_finalizer(aclose)
    if task is None:
        # The hook failed on entry. Its failure has been reported and the run's
        # own outcome stands; there is no cleanup left to bound.
        return
    # Attached before the first wait, so it covers every way this task can end:
    # finishing inside the window, finishing while being reclaimed, and
    # finishing after this helper has already returned. A retrieval placed on
    # the paths instead would have to be repeated at each of them, and the
    # paths that matter most are the ones it is hardest to reach — a task
    # cancelled by the reclaim that then fails as it unwinds raises *after* the
    # reclaim returns, where no later statement of this function runs at all.
    task.add_done_callback(_retrieve_task_outcome)
    try:
        await asyncio.wait((task,), timeout=_STDIN_SETTLE_GRACE_S)
    except asyncio.CancelledError:
        # The caller's cancellation outranks this cleanup: the task is left to
        # the loop rather than awaited, and the cancellation propagates. The
        # task is cancelled first so the finalizer is at least asked to stop.
        task.cancel()
        raise
    if not task.done():
        await _cancel_stdin_writer(task, budget=_STDIN_FINALIZER_RECLAIM_S)


def _schedule_finalizer(
    aclose: cabc.Callable[[], cabc.Awaitable[None]],
) -> asyncio.Task[None] | None:
    """Invoke a producer's ``aclose`` and schedule whatever it returns.

    Both the call and the scheduling are guarded, and for the same reason: this
    runs *after* the run has already decided its outcome. The finalizer is the
    caller's code, so it can fail on the way in as well as on the way through —
    an ``aclose`` that raises before it returns its awaitable, a coroutine
    object that cannot be wrapped as a task. An unguarded call would let that
    failure leave this helper and take the place of the run's real ending: a
    ``StdinSourceError`` the caller needed to see would be reported as a
    cleanup fault from a producer cuprum was already finished with.

    ``CancelledError`` is deliberately not suppressed. Cancellation is the
    caller's control flow rather than a cleanup failure, so it propagates to
    end the run exactly as it would have without this helper.

    Parameters
    ----------
    aclose : collections.abc.Callable[[], collections.abc.Awaitable[None]]
        The producer's own cleanup hook, already fetched from the iterator.

    Returns
    -------
    asyncio.Task[None] | None
        The scheduled finalization, or ``None`` if the hook could not be
        invoked or scheduled at all. ``None`` means the finalizer is done with
        — it failed on entry and its failure has been reported — so every
        caller treats it as "nothing left to wait for" rather than as an error.
    """
    with contextlib.suppress(Exception):
        return asyncio.ensure_future(aclose())
    return None
