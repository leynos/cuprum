"""Waiting on a line stream's readers, and attributing the first failure.

Part of the ``cuprum._line_stream`` package. The post-exit collect in
``cuprum._line_stream.drain`` owns the *budget*: one ``asyncio.timeout``
holding whatever is left of the run's configured deadline, which bounds the
idle-heartbeat stop, the stdin writer, and the reader wait alike.

This module owns only what that budget is spent waiting on — the readers —
together with the attribution question that follows: when several of them end
at once, whose failure ended the run? Keeping it separate leaves the collect
module about *when* the run stops waiting and this one about *what* it learned
while it waited.
"""

from __future__ import annotations

import asyncio
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._subprocess_wait_types import _Consumer, _StreamConsumerTask

__all__ = ["_await_consumers", "_first_reader_failure"]


async def _await_consumers(
    consumers: tuple[_Consumer, _Consumer],
    remaining: float | None,
) -> BaseException | None:
    """Wait for both readers, returning a reader's own failure to the caller.

    Returns when both readers have finished normally, and raises ``TimeoutError``
    when *remaining* elapses with a reader still running — the same exception the
    enclosing scope raises for the awaits that have no bound of their own, so the
    caller has one expiry route rather than two.

    A reader that failed returns its exception instead of raising it. Raising
    inside the caller's deadline scope would hand that exception to the scope's
    ``except TimeoutError`` arm, which cannot tell a reader failure that happens
    to be a ``TimeoutError`` from the run's own expiry; the caller re-raises it
    only after the scope has closed.

    Observing the readers rather than gathering them is what protects a trailing
    line: a reader parked in ``queue.put`` on a full queue is left for the
    reconciliation to cancel, and the reconciliation keeps every prefix already
    captured.

    Returning on the first exception is what preserves failure attribution.
    Waiting for every reader instead would spend the whole budget on a sibling
    that is hanging, and the budget expiring would then be reported as the run's
    own deadline — a timeout handed to the caller in place of the reader failure
    that actually ended the run. Which failure that is comes from completion
    order, recorded as each reader finishes, and not from the set
    ``asyncio.wait`` reports: two readers failing in the same loop iteration are
    otherwise attributed by task hashing, so one run could report a different
    error from another, or the same run a different error from one process to
    the next. A reader's own ``CancelledError`` is skipped, which is deliberate:
    cancelling a reader *is* the run ending, so the wait stays until the deadline
    and reports the expiry the caller configured.

    A stream cuprum holds no pipe for contributes no reader and is skipped. Its
    payload is ``None`` and its slot is restored by the caller's drain decoding,
    which is the only place the positional pair is assembled.

    Returns
    -------
    BaseException | None
        The first non-cancellation failure among the readers, in completion
        order, or ``None`` when they all completed. It is the caller's to
        re-raise, after its deadline scope has closed.

    Raises
    ------
    TimeoutError
        When *remaining* elapses with a reader still unfinished. A budget that
        was already spent when the wait began is reported the same way, because
        ``asyncio.wait`` returns at once for a non-positive timeout and leaves
        every reader that has not finished in *pending*.
    """
    readers = tuple(task for task in consumers if task is not None)
    if not readers:
        return None
    # Completion order, recorded as each reader finishes: a done callback is
    # what sees the actual completion, and ``asyncio.wait`` only reports the
    # set of readers that had finished by the time it was released. The
    # callbacks always outrun that release, because the wait yields to the loop
    # before it can complete — including when every reader was already done.
    finished: list[_StreamConsumerTask] = []
    for task in readers:
        task.add_done_callback(finished.append)
    _, pending = await asyncio.wait(
        readers,
        timeout=remaining,
        return_when=asyncio.FIRST_EXCEPTION,
    )
    failure = _first_reader_failure(finished)
    if failure is not None:
        return failure
    if pending:
        # No reader failed, so what is left is an unfinished reader and a
        # budget that ran out waiting for it — possibly before the wait began,
        # which is the same expiry arriving by a different route. That is the
        # run's own deadline, reported as the same exception the enclosing
        # scope raises.
        raise TimeoutError
    # Every reader finished, and every finished reader's result is discarded:
    # the drains are read once, after this function returns, on the single
    # route a completed run takes to its caller. What this call is for is the
    # remaining re-raise — a reader that ended by its own cancellation.
    await asyncio.gather(*finished)
    return None


def _first_reader_failure(
    finished: cabc.Sequence[_StreamConsumerTask],
) -> BaseException | None:
    """Return a reader's own failure, preferring it to any expiry report.

    The order matters: ``asyncio.wait`` reports readers that finished *and* a
    wait that timed out through the same call, so a sibling's failure and the
    deadline can be true at once. The failure is what ended the run, and
    reporting the deadline instead would hand the caller a timeout in place of
    the error that actually caused it.

    *finished* is a sequence rather than a set for exactly that reason: it is
    the order the readers completed in, so the first entry that failed is the
    reader whose failure ended the run rather than whichever one set iteration
    happened to reach first.

    A reader that ended by its own cancellation is skipped rather than
    returned: cancelling a reader *is* the run ending, and the reconciliation
    the caller falls back to is what settles it.

    Returns
    -------
    BaseException | None
        The first non-cancellation failure among the readers, or ``None`` when
        every reader completed or was cancelled.
    """
    for task in finished:
        if task.cancelled():
            continue
        error = task.exception()
        if error is not None:
            return error
    return None
