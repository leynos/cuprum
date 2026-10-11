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
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import typing as typ

from cuprum._timeout_reporting import (
    _report_capture_eof_grace_expiry,
    _report_teardown_drain_failure,
)

if typ.TYPE_CHECKING:
    from cuprum._subprocess_wait_types import (
        _DrainContext,
        _StreamConsumerTask,
        _StreamPayload,
    )

# A capturing drain gives readers a short bounded chance to observe the EOF
# created by process termination. A grandchild may keep a pipe open, so teardown
# must never wait indefinitely.
_CAPTURE_EOF_GRACE_S = 0.25
# Named explicitly rather than with ``__name__``: the drain used to live in
# ``cuprum._subprocess_wait``, and operators filter on that name to diagnose a
# teardown, so moving the code must not move the channel it reports on.
_DRAIN_LOGGER = logging.getLogger("cuprum._subprocess_wait")


async def _drain_stream_consumers(
    consumers: tuple[_StreamConsumerTask, _StreamConsumerTask],
    context: _DrainContext,
) -> tuple[_StreamPayload | None, _StreamPayload | None]:
    """Cancel pending consumers, drain them once, and report their output.

    A capture-aware drain lets its readers observe EOF before it cancels them,
    then maps an absent result to the empty value so a timed-out capturing run
    always reports something for both streams. Other paths discard output and
    therefore skip the grace window and retain ``None`` for absent text.

    A consumer that drains with an unexpected exception (anything other than the
    ``CancelledError`` produced by cancelling it) is still absorbed to preserve
    the primary timeout or cancellation, but is reported through
    :func:`_report_teardown_drain_failure` — a structured log record plus, when
    ``observation`` is supplied, a best-effort ``teardown_error`` observe event
    — so the drain failure stays observable.

    Returns
    -------
    tuple[_StreamPayload | None, _StreamPayload | None]
        The stdout and stderr payloads, as ``str`` or ``bytes`` according to
        the run's mode. Capturing drains return an empty payload for both
        streams, while other drains report ``None`` for absent output.

    """
    if context.capture:
        await _await_capture_eof_grace(consumers, context)
    stdout_result, stderr_result = await _settle_consumers(
        consumers,
        discard_on_cancel=(None if context.capture else context.discard_on_cancel),
    )
    _report_drain_failures(stdout_result, stderr_result, context)
    stdout_text = _decode_consumer_result(stdout_result, context=context)
    stderr_text = _decode_consumer_result(stderr_result, context=context)
    return stdout_text, stderr_text


async def _await_capture_eof_grace(
    consumers: tuple[_StreamConsumerTask, _StreamConsumerTask],
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
    pending_count = sum(not task.done() for task in consumers)
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
    stdout_result: _StreamPayload | BaseException | None,
    stderr_result: _StreamPayload | BaseException | None,
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
    result: _StreamPayload | BaseException | None,
    *,
    context: _DrainContext,
) -> _StreamPayload | None:
    """Map an absent consumer result to the contract for its drain.

    A reader that already produced a payload was configured for the run's mode
    and is returned untouched. The fallback an absent one gets has to be built
    for that same mode, since a ``bytes``-typed result field cannot carry the
    text-mode ``""``.

    Returns
    -------
    _StreamPayload | None
        The reader's own payload, the mode's empty value when a capturing
        drain lost its reader, or ``None`` when the drain was not capturing.
    """
    if not isinstance(result, BaseException) and result is not None:
        return result
    if not context.capture:
        return None
    return b"" if context.capture_bytes else ""


async def _settle_consumers(
    consumers: tuple[_StreamConsumerTask, ...],
    *,
    discard_on_cancel: asyncio.Event | None = None,
) -> list[_StreamPayload | BaseException | None]:
    """Cancel unfinished consumers and drain every result once."""
    if discard_on_cancel is not None:
        discard_on_cancel.set()
    _cancel_pending_consumers(consumers)
    return await asyncio.gather(*consumers, return_exceptions=True)


async def _await_eof_grace(
    consumers: tuple[_StreamConsumerTask, _StreamConsumerTask],
) -> None:
    """Give readers the production-bounded opportunity to observe EOF."""
    await asyncio.wait(consumers, timeout=_CAPTURE_EOF_GRACE_S)


def _cancel_pending_consumers(
    consumers: tuple[_StreamConsumerTask, ...],
) -> None:
    """Cancel each consumer task that has not already completed."""
    # Finished readers keep their captured output; only tasks still blocked
    # after process termination (or on cancellation) are cancelled, so cleanup
    # cannot hang on a reader wedged on a pipe that never reached EOF.
    for task in consumers:
        if not task.done():
            task.cancel()


__all__ = [
    "_await_capture_eof_grace",
    "_await_eof_grace",
    "_cancel_pending_consumers",
    "_decode_consumer_result",
    "_drain_stream_consumers",
    "_report_drain_failures",
    "_settle_consumers",
]
