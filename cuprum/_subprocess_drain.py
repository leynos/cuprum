"""Reconcile a run's stream consumers exactly once.

Split from ``cuprum._subprocess_wait`` so that module stays about *waiting* —
applying a deadline, observing an exit — while this one owns what happens to
the tasks a run leaves behind: the stdin writer, the two stream readers, and
the per-stream relay diagnostics.

Reconciliation is one unit because its halves have to finish together.
Draining first would leave a writer blocked on a pipe nobody is reading, and
shielding the halves separately would let a cancellation landing between them
strand the rest. :func:`_reconcile_run_tasks` therefore performs all of it, so
its callers can wrap the whole thing in ``_shielded_cleanup`` and know every
part runs.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import contextlib
import dataclasses as dc
import logging
import typing as typ

from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._subprocess_stdin import _cancel_stdin_writer
from cuprum._timeout_reporting import (
    _report_capture_eof_grace_expiry,
    _report_teardown_drain_failure,
)

if typ.TYPE_CHECKING:
    from cuprum._idle_heartbeat import _IdleMonitor
    from cuprum._pipeline_types import _StageObservation
    from cuprum._streams import _RelayDiagnostics


# A capturing drain gives readers a short bounded chance to observe the EOF
# created by process termination. A grandchild may keep a pipe open, so teardown
# must never wait indefinitely.
_CAPTURE_EOF_GRACE_S = 0.25
_DRAIN_LOGGER = logging.getLogger(__name__)

type _EofGraceWaiter = cabc.Callable[
    [tuple[asyncio.Task[str | None], asyncio.Task[str | None]]],
    cabc.Awaitable[object],
]


type _EofGraceWaiter = cabc.Callable[
    [tuple[asyncio.Task[str | None], asyncio.Task[str | None]]],
    cabc.Awaitable[object],
]


@dc.dataclass(frozen=True, slots=True)
class _DrainContext:
    """Capture and observability context for one consumer drain."""

    capture: bool
    eof_grace_waiter: _EofGraceWaiter | None = None
    pid: int | None = None
    observation: _StageObservation | None = None
    discard_on_cancel: asyncio.Event | None = None


@dc.dataclass(frozen=True, slots=True)
class _RunTaskOwnership:
    """The stdin writer, stream consumers, and diagnostics owned by one run.

    ``relay_diagnostics`` holds the per-stream collectors handed to the
    consumer drains, so the run's one reconciliation point — success gather or
    teardown drain — reads the result diagnostics from the tasks it already
    settles rather than inspecting them a second time.
    """

    stdin_task: asyncio.Task[None] | None
    consumers: tuple[asyncio.Task[str | None], asyncio.Task[str | None]]
    discard_on_cancel: asyncio.Event
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics]
    idle: _IdleMonitor | None = None


async def _await_eof_grace(
    consumers: tuple[asyncio.Task[str | None], asyncio.Task[str | None]],
) -> None:
    """Give readers the production-bounded opportunity to observe EOF."""
    await asyncio.wait(consumers, timeout=_CAPTURE_EOF_GRACE_S)


def _cancel_pending_consumers(
    consumers: tuple[asyncio.Task[str | None], ...],
) -> None:
    """Cancel each consumer task that has not already completed."""
    # Finished readers keep their captured output; only tasks still blocked
    # after process termination (or on cancellation) are cancelled, so cleanup
    # cannot hang on a reader wedged on a pipe that never reached EOF.
    for task in consumers:
        if not task.done():
            task.cancel()


async def _drain_stream_consumers(
    consumers: tuple[asyncio.Task[str | None], asyncio.Task[str | None]],
    context: _DrainContext,
) -> tuple[str | None, str | None]:
    """Cancel pending consumers, drain them once, and decode their output.

    A capture-aware drain lets its readers observe EOF before it cancels them,
    then maps an absent result to the empty string so a timed-out capturing run
    always reports text. Other paths discard output and therefore skip the
    grace window and retain ``None`` for absent text.

    A consumer that drains with an unexpected exception (anything other than the
    ``CancelledError`` produced by cancelling it) is still absorbed to preserve
    the primary timeout or cancellation, but is reported through
    :func:`_report_teardown_drain_failure` — a structured log record plus, when
    ``observation`` is supplied, a best-effort ``teardown_error`` observe event
    — so the drain failure stays observable.

    Returns
    -------
    tuple[str | None, str | None]
        The decoded stdout and stderr text. Capturing drains return text for
        both streams, while other drains report ``None`` for absent text.

    """
    if context.capture:
        await _await_capture_eof_grace(consumers, context)
    stdout_result, stderr_result = await _settle_consumers(
        consumers,
        discard_on_cancel=(None if context.capture else context.discard_on_cancel),
    )
    _report_drain_failures(stdout_result, stderr_result, context)
    stdout_text = _decode_consumer_result(stdout_result, capture=context.capture)
    stderr_text = _decode_consumer_result(stderr_result, capture=context.capture)
    return stdout_text, stderr_text


async def _await_capture_eof_grace(
    consumers: tuple[asyncio.Task[str | None], asyncio.Task[str | None]],
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
    stdout_result: str | BaseException | None,
    stderr_result: str | BaseException | None,
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
    result: str | BaseException | None,
    *,
    capture: bool,
) -> str | None:
    """Map an absent consumer result to the contract for its drain."""
    if isinstance(result, BaseException) or result is None:
        return "" if capture else None
    return result


async def _settle_consumers(
    consumers: tuple[asyncio.Task[str | None], ...],
    *,
    discard_on_cancel: asyncio.Event | None = None,
) -> list[str | BaseException | None]:
    """Cancel unfinished consumers and drain every result once."""
    if discard_on_cancel is not None:
        discard_on_cancel.set()
    _cancel_pending_consumers(consumers)
    return await asyncio.gather(*consumers, return_exceptions=True)


async def _reconcile_run_tasks(
    tasks: _RunTaskOwnership,
    context: _DrainContext,
) -> tuple[str | None, str | None]:
    """Stop the idle heartbeat, cancel the stdin writer, then drain the streams.

    The stream consumers drain with ``return_exceptions=True``, so their
    already-recorded diagnostics survive the cancellation that a teardown
    performs: a cancelled reader keeps the fallback it recorded before it was
    cancelled.

    The halves are one unit so a caller can run them under
    :func:`_shielded_cleanup` and know all of them finish: draining first would
    leave a writer blocked on a pipe nobody is reading, and shielding them
    separately would let a cancellation landing between two of them strand the
    rest.

    The heartbeat goes first. Reconciliation runs once the run is already
    ending, and a keepalive announcing that a terminated child is "still
    running" is worse than no keepalive at all. Stopping is idempotent, so the
    run's other exit paths can call it too.

    Returns
    -------
    tuple[str | None, str | None]
        The decoded stdout and stderr text, as produced by
        :func:`_drain_stream_consumers`.
    """
    await _stop_idle_monitor(tasks.idle)
    await _cancel_stdin_writer(tasks.stdin_task)
    stdout_text, stderr_text = await _drain_stream_consumers(
        tasks.consumers,
        context,
    )
    for diagnostics in tasks.relay_diagnostics:
        diagnostics.settle()
    return stdout_text, stderr_text


__all__ = [
    "_DrainContext",
    "_RunTaskOwnership",
    "_await_capture_eof_grace",
    "_await_eof_grace",
    "_cancel_pending_consumers",
    "_decode_consumer_result",
    "_drain_stream_consumers",
    "_reconcile_run_tasks",
    "_report_drain_failures",
    "_settle_consumers",
]
