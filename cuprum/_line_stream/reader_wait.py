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

Two reader outcomes that a set of settled tasks makes easy to miss are
classified here. A reader that ended *cancelled* while the run was still live
is a failure, not the run ending: its pipe is undrained, so the child can block
on a full buffer exactly as the #551 failure did. A reader that returned while
preserving a partial capture is the same failure spelled differently. Both are
reported through ``ConsumerCancelledError`` for their stream, so the
coordinator can publish an error the caller can tell from its own
cancellation.
"""

from __future__ import annotations

import typing as typ

from cuprum._subprocess_rendezvous import _await_consumer_outcomes

if typ.TYPE_CHECKING:
    from cuprum._subprocess_wait_types import _Consumer

__all__ = ["_await_consumers"]


async def _await_consumers(
    consumers: tuple[_Consumer, _Consumer],
    remaining: float | None,
) -> BaseException | None:
    """Wait for both readers, returning the first failure in completion order.

    Returns when both readers have finished cleanly, and raises ``TimeoutError``
    when *remaining* elapses with a reader still running — the same exception the
    enclosing scope raises for the awaits that have no bound of their own, so the
    caller has one expiry route rather than two.

    A reader that failed returns its outcome instead of raising it. Raising
    inside the caller's deadline scope would hand that exception to the scope's
    ``except TimeoutError`` arm, which cannot tell a reader failure that happens
    to be a ``TimeoutError`` from the run's own expiry; the caller re-raises it
    only after the scope has closed.

    Observing the readers rather than gathering them is what protects a trailing
    line: a reader parked in ``queue.put`` on a full queue is left for the
    reconciliation to cancel, and the reconciliation keeps every prefix already
    captured.

    Classifying on the first completed reader is what preserves failure
    attribution. Waiting for every reader instead would spend the whole budget
    on a sibling that is hanging, and the budget expiring would then be reported
    as the run's own deadline — a timeout handed to the caller in place of the
    reader failure that actually ended the run. Which failure that is comes from
    completion order, recorded as each reader finishes, and not from the set
    ``asyncio.wait`` reports: two readers failing in the same loop iteration are
    otherwise attributed by task hashing, so one run could report a different
    error from another, or the same run a different error from one process to
    the next. A reader that ended cancelled, or returned a partial capture, is
    classified as a failure for its stream rather than skipped: the supervisor
    is still live, so the cancellation came from a callback or from something
    that cancelled the reader, not from the caller cancelling this run.

    A stream cuprum holds no pipe for contributes no reader and is skipped. Its
    payload is ``None`` and its slot is restored by the caller's drain decoding,
    which is the only place the positional pair is assembled.

    Returns
    -------
    BaseException | None
        The first failure among the readers, in completion order, or ``None``
        when they all completed cleanly. A cancelled or short-stopped reader is
        reported through
        :class:`~cuprum._stream_consumer_errors.ConsumerCancelledError` for its
        stream.

    Raises
    ------
    TimeoutError
        When *remaining* elapses with a reader still unfinished. A budget that
        was already spent when the wait began is reported the same way, because
        ``asyncio.wait`` returns at once for a non-positive timeout and leaves
        every reader that has not finished in *pending*.
    """  # ruff: ignore[docstring-extraneous-exception] - the shared wait raises the expiry.
    # The shared wait owns completion order, the stream labels, and the
    # cancellation classification; this module owns only the reader-specific
    # budget and the attribution contract its caller reads.
    return await _await_consumer_outcomes(consumers, budget=remaining)
