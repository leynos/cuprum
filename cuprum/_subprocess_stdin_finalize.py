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

from cuprum._subprocess_stdin import _STDIN_SETTLE_GRACE_S, _cancel_stdin_writer

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
        If the surrounding run is cancelled while the bounded wait is pending.
        Cancellation is the caller's control flow, so it is never absorbed by
        the window.
    """
    if source is None:
        return
    aclose = getattr(source, "aclose", None)
    if aclose is None:
        return
    task = asyncio.ensure_future(aclose())
    try:
        await asyncio.wait((task,), timeout=_STDIN_SETTLE_GRACE_S)
    except asyncio.CancelledError:
        # The caller's cancellation outranks this cleanup: the task is left to
        # the loop rather than awaited, and the cancellation propagates. The
        # task is cancelled first so the finalizer is at least asked to stop.
        task.cancel()
        raise
    if not task.done():
        await _cancel_stdin_writer(task)
        return
    if task.cancelled():
        return
    # Awaited so a failure the finalizer raised is retrieved here rather than
    # reported later as an unretrieved task exception. It cannot change the
    # run's outcome, which is already decided.
    with contextlib.suppress(Exception):
        await task
