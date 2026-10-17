"""Running one drain, and the empty capture an absent reader owes its caller.

The consumer-side half of stream handling: a drain reads a child's pipe to EOF
(or is cancelled partway), fans each chunk out to capture, echo, and any chunk
sink, and reports what it captured in the mode its config asked for. The
teardown paths a run ends on — a timeout, an external cancellation, a
reconciliation after a failure — all funnel into :func:`_drain_stream_consumers`
so a reader is cancelled and settled exactly once.

Split from ``cuprum._streams``, which owns the *reading* machinery this module
drives: the config a drain is built from, the chunk loop, and the per-chunk fan
out. This module owns the three rules that are about *finishing* rather than
reading — what an absent reader reports, how a capturing drain's bounded EOF
grace behaves when it expires, and how a reader failure is reported without
replacing the error that ended the run.

The distinction matters because the fallback is mode-dependent. A cancelled,
failed, or never-started reader still owes its caller the empty value of the
mode the run is in — ``b""`` for a byte-exact run, ``""`` for a text one —
and getting that wrong would hand a ``bytes``-typed result field a ``str``.

A slot in the consumers pair may also be ``None``, for a stream cuprum holds
no pipe for because the caller redirected it to a file or a borrowed
descriptor. Those slots are absent readers rather than missing ones: every
helper here keeps them in place and settles them to an absent result, so a
redirected stdout cannot shift stderr's payload into its position.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import contextlib
import logging
import typing as typ

from cuprum._stream_drain_finish import _CapturedDrain, _empty_capture_for
from cuprum._timeout_reporting import (
    _report_capture_eof_grace_expiry,
    _report_teardown_drain_failure,
)

if typ.TYPE_CHECKING:
    from cuprum._subprocess_wait_types import (
        _Consumer,
        _ConsumerPair,
        _DrainContext,
        _DrainPair,
    )
    from cuprum.capture_policy import CaptureStreamName

# The two streams in the order every drain pair uses, so the fallback builder
# can be indexed by the same integer that selects a run's policy for it.
_STREAM_NAMES: tuple[CaptureStreamName, CaptureStreamName] = ("stdout", "stderr")

# A capturing drain gives readers a short bounded chance to observe the EOF
# created by process termination. A grandchild may keep a pipe open, so teardown
# must never wait indefinitely.
_CAPTURE_EOF_GRACE_S = 0.25
# Named explicitly rather than with ``__name__``: the drain used to live in
# ``cuprum._subprocess_wait``, and operators filter on that name to diagnose a
# teardown, so moving the code must not move the channel it reports on.
_DRAIN_LOGGER = logging.getLogger("cuprum._subprocess_wait")


def _consumer_awaitable(
    task: _Consumer,
) -> asyncio.Task[_CapturedDrain] | asyncio.Future[None]:
    """Return a settled stand-in for a stream that has no consumer task.

    :func:`_settle_consumers` gathers its inputs positionally — index ``0`` is
    stdout's result, index ``1`` is stderr's — so a stream cuprum holds no pipe
    for must still contribute a slot rather than being filtered out, which
    would shift the stderr result into stdout's place. A future resolved to
    ``None`` is indistinguishable from a consumer that captured nothing, which
    is what such a stream reports anyway: :func:`_decode_consumer_result` maps
    ``None`` to the empty value of the run's mode for a capturing drain and to
    ``None`` for a drain that discards output.

    Returns
    -------
    asyncio.Task[_CapturedDrain] | asyncio.Future[None]
        The task itself when there is one, otherwise a future already settled
        to ``None`` so the gather still sees a slot for this stream.
    """
    if task is not None:
        return task
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    future.set_result(None)
    return future


async def _drain_stream_consumers(
    consumers: _ConsumerPair,
    context: _DrainContext,
) -> _DrainPair:
    """Cancel pending consumers, drain them once, and report what they captured.

    A capture-aware drain lets its readers observe EOF before it cancels them,
    then maps an absent result to the empty capture so a timed-out capturing run
    always reports something for both streams. Other paths discard output and
    therefore skip the grace window and retain nothing for absent text.

    A consumer that drains with an unexpected exception (anything other than the
    ``CancelledError`` produced by cancelling it) is still absorbed to preserve
    the primary timeout or cancellation, but is reported through
    :func:`_report_teardown_drain_failure` — a structured log record plus, when
    ``observation`` is supplied, a best-effort ``teardown_error`` observe event
    — so the drain failure stays observable.

    Returns
    -------
    _DrainPair
        The stdout and stderr results, each a payload with the record
        describing it. A capturing drain reports an empty capture for a stream
        it lost a reader on, while a drain that was not capturing reports a
        wholly empty result.

    """
    if context.capturing:
        await _await_capture_eof_grace(consumers, context)
    stdout_result, stderr_result = await _settle_consumers(
        consumers,
        discard_on_cancel=(None if context.capturing else context.discard_on_cancel),
    )
    _report_drain_failures(stdout_result, stderr_result, context)
    stdout_drain = _decode_consumer_result(stdout_result, stream=0, context=context)
    stderr_drain = _decode_consumer_result(stderr_result, stream=1, context=context)
    return stdout_drain, stderr_drain


async def _await_capture_eof_grace(
    consumers: _ConsumerPair,
    context: _DrainContext,
) -> None:
    """Give capturing consumers their bounded opportunity to reach EOF."""
    try:
        await (context.eof_grace_waiter or _await_eof_grace)(consumers)
    except asyncio.CancelledError:
        with contextlib.suppress(asyncio.CancelledError):
            await _settle_consumers(
                consumers, discard_on_cancel=context.discard_on_cancel
            )
        raise
    pending_count = sum(task is not None and not task.done() for task in consumers)
    if pending_count:
        _DRAIN_LOGGER.debug(
            "capture_eof_grace_expired pending_readers=%s",
            pending_count,
            extra={
                "cuprum_pending_readers": pending_count,
                "cuprum_eof_grace_s": _CAPTURE_EOF_GRACE_S,
            },
        )
        _report_capture_eof_grace_expiry(
            context.observation,
            pid=context.pid,
            eof_grace_s=_CAPTURE_EOF_GRACE_S,
            pending_readers=pending_count,
        )


def _report_drain_failures(
    stdout_result: _CapturedDrain | BaseException | None,
    stderr_result: _CapturedDrain | BaseException | None,
    context: _DrainContext,
) -> None:
    """Report unexpected consumer failures without replacing the primary error."""
    results = (("stdout", stdout_result), ("stderr", stderr_result))
    drain_errors = tuple(
        type(result).__name__
        for _, result in results
        if isinstance(result, BaseException)
        and not isinstance(result, asyncio.CancelledError)
    )
    if drain_errors:
        _report_teardown_drain_failure(
            context.observation, pid=context.pid, error_types=drain_errors
        )
    for stream, result in results:
        if isinstance(result, BaseException) and not isinstance(
            result, asyncio.CancelledError
        ):
            _DRAIN_LOGGER.debug(
                "stream_consumer_failed stream=%s error=%s",
                stream,
                type(result).__name__,
                extra={
                    "cuprum_operation": f"drain_{stream}",
                    "cuprum_error_type": type(result).__name__,
                },
            )


def _decode_consumer_result(
    result: _CapturedDrain | BaseException | None,
    *,
    stream: int,
    context: _DrainContext,
) -> _CapturedDrain:
    """Map a settled consumer task to the contract for its drain.

    A reader that already produced a capture was configured for the run's mode
    and is returned untouched. The fallback an absent one gets has to be built
    for that same mode, since a ``bytes``-typed result field cannot carry the
    text-mode ``""`` — and it has to carry a *record* as well as a payload, so
    that a caller cannot tell a lost reader from a child that wrote nothing by
    the shape of what it receives.

    The fallback is decided *per stream*: a run may capture one pipe and not the
    other, and a stream that was never captured owes an empty result rather than
    an empty payload, which would claim output existed where no reader ever ran.
    *stream* indexes the run's policies to make that choice.

    Parameters
    ----------
    result : _CapturedDrain | BaseException | None
        What the consumer task settled to.
    stream : int
        Which of the two streams *result* belongs to.
    context : _DrainContext
        The drain this result belongs to: its policies decide whether an
        absent reader owes an empty capture or nothing at all, and its mode
        decides what that empty capture's payload type is.

    Returns
    -------
    _CapturedDrain
        The reader's own result, the mode's empty capture when a capturing
        drain lost its reader, or a wholly empty result when the drain was not
        capturing.
    """
    if not isinstance(result, BaseException) and result is not None:
        return result
    return _empty_capture_for(
        policy=context.captures[stream],
        stream=_STREAM_NAMES[stream],
        capture_bytes=context.capture_bytes,
    )


def _decode_consumer_drains(
    results: cabc.Sequence[_CapturedDrain | BaseException | None],
    *,
    context: _DrainContext,
) -> _DrainPair:
    """Decode both settled streams of a completed gather, in stream order.

    A stream cuprum holds no pipe for is still one of two positions: its slot
    settles to ``None`` and must report *that stream's* empty capture, or a
    redirected stdout would hand stderr's result to stdout's caller. The two
    reads are one function so no call site can decode one slot and forget the
    other, and so the stream indexes stay beside the names they select.

    Returns
    -------
    _DrainPair
        The stdout and stderr results, each the reader's own capture or the
        empty capture its configured policy owed when no reader ran.
    """
    return (
        _decode_consumer_result(results[0], stream=0, context=context),
        _decode_consumer_result(results[1], stream=1, context=context),
    )


async def _settle_consumers(
    consumers: tuple[_Consumer, ...],
    *,
    discard_on_cancel: asyncio.Event | None = None,
) -> list[_CapturedDrain | BaseException | None]:
    """Cancel unfinished consumers and drain every result once.

    A stream cuprum holds no pipe for contributes a settled ``None`` rather
    than being skipped, which keeps each result in its own stream's position.

    Returns
    -------
    list[_CapturedDrain | BaseException | None]
        One settled entry per input, in input order: the drain result, the
        exception a consumer raised, or ``None`` for a stream with no consumer.
    """
    if discard_on_cancel is not None:
        discard_on_cancel.set()
    _cancel_pending_consumers(consumers)
    return await asyncio.gather(
        *(_consumer_awaitable(task) for task in consumers),
        return_exceptions=True,
    )


async def _await_eof_grace(consumers: _ConsumerPair) -> None:
    """Give readers the production-bounded opportunity to observe EOF."""
    await asyncio.wait(
        [task for task in consumers if task is not None],
        timeout=_CAPTURE_EOF_GRACE_S,
    )


def _cancel_pending_consumers(consumers: tuple[_Consumer, ...]) -> None:
    """Cancel each consumer task that has not already completed."""
    # Finished readers keep their captured output; only tasks still blocked
    # after process termination (or on cancellation) are cancelled, so cleanup
    # cannot hang on a reader wedged on a pipe that never reached EOF.
    for task in consumers:
        if task is not None and not task.done():
            task.cancel()


__all__ = [
    "_await_capture_eof_grace",
    "_await_eof_grace",
    "_cancel_pending_consumers",
    "_consumer_awaitable",
    "_decode_consumer_drains",
    "_decode_consumer_result",
    "_drain_stream_consumers",
    "_report_drain_failures",
    "_settle_consumers",
]
