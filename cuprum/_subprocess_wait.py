"""Waiting for subprocess exit, and reconciling its stream consumers.

Split from ``cuprum._subprocess_execution`` so the runner module is about
orchestration — spawning, wiring streams, assembling the result — while the
rules for *ending* a run live here: how the stream consumers are drained
exactly once, how a stdin writer's failure is raced against the exit wait, and
how the stdin writer is cancelled alongside them.

The other halves of ending a run — how long to wait for the child, what a
deadline expiry does to it, and which exception a cancelled wait raises — are
``cuprum._subprocess_deadline``'s; and the race that ends a run on a stdin
failure rather than on the child's exit is ``cuprum._subprocess_rendezvous``'s.
This module imports the helpers it needs from both and re-exports them, so the
split stays an internal arrangement: ``cuprum._subprocess_wait`` remains the
one import path for every caller and for the test suite's monkeypatch seams.

The task reconciliation a run ends with is owned by ``_reconcile_run_tasks``
so its callers can run it under ``_shielded_cleanup`` as one unit.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import contextlib
import dataclasses as dc
import logging
import typing as typ

from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._subprocess_deadline import (
    _wait_for_exit_code,
    _wait_for_exit_code_within_timeout,
)
from cuprum._subprocess_rendezvous import _await_exit_or_writer_failure
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

type _EofGraceWaiter = cabc.Callable[[_ConsumerPair], cabc.Awaitable[object]]


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
    consumers: _ConsumerPair
    discard_on_cancel: asyncio.Event
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics]
    idle: _IdleMonitor | None = None


# A run's two consumers, in stdout-then-stderr order. A ``None`` slot is a
# stream cuprum holds no pipe for — a redirected one — and carries no task,
# because attaching a reader to a descriptor the library does not own is the
# one thing the spawn layer must never do.
type _Consumer = asyncio.Task[str | None] | None
type _ConsumerPair = tuple[_Consumer, _Consumer]


def _consumer_awaitable(
    task: _Consumer,
) -> asyncio.Task[str | None] | asyncio.Future[str | None]:
    """Return a settled stand-in for a stream that has no consumer task.

    :func:`_settle_consumers` gathers its inputs positionally — index ``0`` is
    stdout's result, index ``1`` is stderr's — so a stream cuprum holds no pipe
    for must still contribute a slot rather than being filtered out, which
    would shift the stderr result into stdout's place. A future resolved to
    ``None`` is indistinguishable from a consumer that captured nothing, which
    is what such a stream reports anyway: :func:`_decode_consumer_result` maps
    ``None`` to the empty string for a capturing run and to ``None`` for a
    drain that discards output.

    Returns
    -------
    asyncio.Task[str | None] | asyncio.Future[str | None]
        The task itself when there is one, otherwise a future already settled
        to ``None`` so the gather still sees a slot for this stream.
    """
    if task is not None:
        return task
    future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
    future.set_result(None)
    return future


async def _await_eof_grace(consumers: _ConsumerPair) -> None:
    """Give readers the production-bounded opportunity to observe EOF."""
    await asyncio.wait(
        [task for task in consumers if task is not None],
        timeout=_CAPTURE_EOF_GRACE_S,
    )


def _cancel_pending_consumers(
    consumers: tuple[_Consumer, ...],
) -> None:
    """Cancel each consumer task that has not already completed."""
    # Finished readers keep their captured output; only tasks still blocked
    # after process termination (or on cancellation) are cancelled, so cleanup
    # cannot hang on a reader wedged on a pipe that never reached EOF.
    for task in consumers:
        if task is not None and not task.done():
            task.cancel()


async def _drain_stream_consumers(
    consumers: _ConsumerPair,
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
    consumers: tuple[_Consumer, ...],
    *,
    discard_on_cancel: asyncio.Event | None = None,
) -> list[str | BaseException | None]:
    """Cancel unfinished consumers and drain every result once.

    A stream cuprum holds no pipe for contributes a settled ``None`` rather
    than being skipped, which keeps each result in its own stream's position.

    Returns
    -------
    list[str | BaseException | None]
        One settled entry per input, in input order: the captured text, the
        exception a consumer raised, or ``None`` for a stream with no consumer.
    """
    if discard_on_cancel is not None:
        discard_on_cancel.set()
    _cancel_pending_consumers(consumers)
    return await asyncio.gather(
        *(_consumer_awaitable(task) for task in consumers),
        return_exceptions=True,
    )


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
    # Re-exported from ``cuprum._subprocess_rendezvous``, which owns the race
    # that ends a run on a stdin failure. Kept in this module's namespace so
    # the single-command run, the line-stream coordinator, and the timeout test
    # modules keep one import path. Sorted into place, not grouped, because the
    # repository's linter requires this list to stay isort-ordered.
    "_await_exit_or_writer_failure",
    "_cancel_pending_consumers",
    "_decode_consumer_result",
    "_drain_stream_consumers",
    "_reconcile_run_tasks",
    "_report_drain_failures",
    "_settle_consumers",
    # Re-exported from ``cuprum._subprocess_deadline``, which owns the child's
    # exit wait, for the same one-import-path reason as the name above.
    "_wait_for_exit_code",
    "_wait_for_exit_code_within_timeout",
]
