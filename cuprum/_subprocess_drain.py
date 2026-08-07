"""Reconciliation of a command's stdout/stderr consumer tasks.

Every teardown path in :mod:`cuprum._subprocess_execution` — timeout,
cancellation, and an unexpected stdin-writer failure — owns two reader tasks it
must settle exactly once before the failure it is cleaning up after continues on
its way. This module is that single settlement point: it cancels whatever is
still pending, drains the tasks once, and decodes each result into the text its
stream reports under the run's capture contract.

Because it runs while a failure is already propagating, it can neither raise nor
report what it discards. What it can do is say so: a reader that failed, and a
grace window that expired with readers still parked, are both recorded at DEBUG
before the drain moves on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

_LOGGER = logging.getLogger(__name__)

# How long a capturing drain lets readers observe EOF before cancelling them.
# The process is already dead by then, so EOF is imminent rather than
# hypothetical, and a reader cancelled a scheduling turn short of it loses the
# capture it was about to deliver. The window stays short because a grandchild
# holding the pipe can wedge a reader indefinitely.
_CAPTURE_EOF_GRACE_S = 0.25


def _cancel_pending_consumers(
    consumers: tuple[asyncio.Task[str | None], ...],
) -> None:
    """Cancel each consumer task that has not already completed.

    Finished readers keep their captured output; only tasks still blocked
    after process termination (or on cancellation) are cancelled, so cleanup
    cannot hang on a reader wedged on a pipe that never reached EOF.
    """
    for task in consumers:
        if not task.done():
            task.cancel()


def _decode_consumer_result(
    result: str | BaseException | None,
    *,
    capture: bool,
) -> str | None:
    """Decode one drained consumer into the text its stream reports.

    A capturing reader keeps whatever it had buffered even when cancellation
    cut its read short (see :func:`cuprum._streams._drain`), so the exception
    branch is reached only by a reader that genuinely failed. Such a reader has
    no text of its own, as has one that was never capturing: a capturing run
    promised a string, so it reports the empty string; a non-capturing run
    reports ``None``, having never had text to report.
    """
    match result:
        case BaseException() | None:
            return "" if capture else None
        case str():
            return result


async def _settle_consumers(
    consumers: tuple[asyncio.Task[str | None], ...],
) -> list[str | BaseException | None]:
    """Cancel every unfinished consumer and drain them all exactly once."""
    _cancel_pending_consumers(consumers)
    return await asyncio.gather(*consumers, return_exceptions=True)


async def _await_eof_grace(
    consumers: tuple[asyncio.Task[str | None], ...],
) -> None:
    """Give readers a bounded window to reach EOF before they are cancelled."""
    try:
        _, pending = await asyncio.wait(consumers, timeout=_CAPTURE_EOF_GRACE_S)
    except asyncio.CancelledError:
        # ``asyncio.wait`` does not cancel what it waits on, so a caller
        # cancelling during the grace window would otherwise leave both
        # readers running and unawaited. Settle them, then re-raise so the
        # cancellation the caller asked for still propagates. Suppressing a
        # second cancellation is enough here, unlike the shielded teardowns
        # in ``_process_lifecycle``: the readers are cancelled synchronously
        # by the settlement below, so they settle whether or not this await
        # survives, and no OS process is left behind if it does not.
        with contextlib.suppress(asyncio.CancelledError):
            await _settle_consumers(consumers)
        raise
    if pending:
        # A reader still parked here is about to be cancelled, and whatever it
        # was waiting to read is lost. That is a wedged pipe — commonly a
        # grandchild that inherited it — rather than a slow one.
        _LOGGER.debug(
            "capture_eof_grace_expired pending_readers=%s timeout_s=%s",
            len(pending),
            _CAPTURE_EOF_GRACE_S,
            extra={
                "cuprum_pending_readers": len(pending),
                "cuprum_timeout_s": _CAPTURE_EOF_GRACE_S,
            },
        )


def _log_discarded_consumer_failure(operation: str, result: object) -> None:
    """Record a reader failure the drain absorbs to protect the primary error.

    A plain cancellation is the drain's own doing and says nothing, but any
    other exception is a reader that broke; decoding turns it into text that
    cannot be told from an empty stream, so it is recorded before it goes.
    """
    if not isinstance(result, BaseException):
        return
    if type(result) is asyncio.CancelledError:
        return
    _LOGGER.debug(
        "stream_consumer_failed operation=%s error=%s",
        operation,
        type(result).__name__,
        exc_info=(type(result), result, result.__traceback__),
        extra={
            "cuprum_operation": operation,
            "cuprum_error_type": type(result).__name__,
        },
    )


async def _drain_stream_consumers(
    consumers: tuple[asyncio.Task[str | None], asyncio.Task[str | None]],
    *,
    capture: bool,
) -> tuple[str | None, str | None]:
    """Settle pending consumers, drain them once, and decode their output.

    Draining exactly once keeps the timeout and cancellation paths from
    reconciling the same tasks twice. Under ``capture`` the readers first get a
    bounded window to reach EOF (see :data:`_CAPTURE_EOF_GRACE_S`), and one that
    still has no text reports the empty string. Pass ``capture=False`` where the
    drained text is discarded, so teardown pays neither window nor contract.
    """
    if capture:
        await _await_eof_grace(consumers)
    stdout_result, stderr_result = await _settle_consumers(consumers)
    for operation, result in (
        ("drain_stdout", stdout_result),
        ("drain_stderr", stderr_result),
    ):
        _log_discarded_consumer_failure(operation, result)
    return (
        _decode_consumer_result(stdout_result, capture=capture),
        _decode_consumer_result(stderr_result, capture=capture),
    )


__all__ = [
    "_CAPTURE_EOF_GRACE_S",
    "_await_eof_grace",
    "_cancel_pending_consumers",
    "_decode_consumer_result",
    "_drain_stream_consumers",
    "_log_discarded_consumer_failure",
    "_settle_consumers",
]
