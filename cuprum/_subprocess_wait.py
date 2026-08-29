"""Waiting for subprocess exit, and reconciling its stream consumers.

Split from ``cuprum._subprocess_execution`` so the runner module is about
orchestration — spawning, wiring streams, assembling the result — while the
rules for *ending* a run live here: how a deadline is applied, when the
process is terminated, and how the stream consumers are drained exactly once.

Termination goes through ``_terminate_all_shielded`` rather than
``_terminate_process`` directly, so a caller cancelling during the grace
period cannot skip the ``SIGKILL`` escalation and strand a child. The task
reconciliation a run ends with is likewise owned by ``_reconcile_run_tasks``
so its callers can run it under ``_shielded_cleanup`` as one unit.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import contextlib
import dataclasses as dc
import logging
import time
import typing as typ

from cuprum._process_exit import _await_process_exit
from cuprum._process_lifecycle import _terminate_all_shielded
from cuprum._subprocess_stdin import _cancel_stdin_writer
from cuprum._subprocess_timeout import _require_timeout
from cuprum._timeout_reporting import (
    _report_capture_eof_grace_expiry,
    _report_teardown_drain_failure,
    _report_timeout_expiry,
)

if typ.TYPE_CHECKING:
    from cuprum._pipeline_types import _StageObservation
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.sh import ExecutionContext


# A capturing drain gives readers a short bounded chance to observe the EOF
# created by process termination. A grandchild may keep a pipe open, so teardown
# must never wait indefinitely.
_CAPTURE_EOF_GRACE_S = 0.25
_DRAIN_LOGGER = logging.getLogger("cuprum._subprocess_drain")

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


@dc.dataclass(frozen=True, slots=True)
class _RunTaskOwnership:
    """The stdin writer and stream consumers owned by one streamed run."""

    stdin_task: asyncio.Task[None] | None
    consumers: tuple[asyncio.Task[str | None], asyncio.Task[str | None]]


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


async def _wait_for_exit_code(
    process: asyncio.subprocess.Process,
    ctx: ExecutionContext,
) -> tuple[int, float]:
    """Wait for a subprocess exit code, terminating it on expiry or cancel.

    Waiting for the exit code is this helper's sole responsibility. Any stream
    consumers belong to the caller, which drains them exactly once when the wait
    fails (see :func:`_run_subprocess_with_streams`); terminating the process
    here lets those consumers reach EOF during that drain.

    Callers also own the deadline: wrap the call in ``async with
    asyncio.timeout(...)`` to bound the wait. A deadline expiry cancels this
    task, so it arrives here as :class:`asyncio.CancelledError` and is torn
    down identically to an externally requested cancellation. The enclosing
    ``asyncio.timeout`` block re-raises expiry as :class:`TimeoutError` once the
    teardown re-raises, while a genuine external cancellation propagates
    unchanged.

    Returns
    -------
    tuple[int, float]
        The process exit code and the ``perf_counter`` timestamp of exit.

    Raises
    ------
    asyncio.CancelledError
        If the wait is cancelled, whether by a caller's deadline expiring or
        by an external cancellation. The process is terminated first.
    """
    try:
        exit_code = await _await_process_exit(process)
    except asyncio.CancelledError:
        # A deadline expiry (via asyncio.timeout) and an external cancellation
        # both surface here as CancelledError and need the same teardown:
        # terminate the process so the caller's drain can reach EOF, then
        # re-raise so the cancellation can propagate.
        #
        # Shielded, because this teardown is itself interruptible. A deadline
        # expiry has already consumed one cancellation, so the caller's next
        # ``cancel()`` lands on the grace-period wait here and would skip the
        # ``SIGKILL`` escalation, leaving a ``SIGTERM``-immune child running.
        await _terminate_all_shielded((process,), ctx.cancel_grace)
        raise
    exited_at = time.perf_counter()
    return exit_code, exited_at


async def _drain_stream_consumers(
    consumers: tuple[asyncio.Task[str | None], asyncio.Task[str | None]],
    context: _DrainContext | None = None,
    *,
    capture: bool | None = None,
    **options: object,
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

    Raises
    ------
    asyncio.CancelledError
        If cancellation arrives while the capture grace is active.
    """
    if context is None:
        context = _DrainContext(
            typ.cast("bool", capture),
            eof_grace_waiter=typ.cast(
                "_EofGraceWaiter | None", options.get("eof_grace_waiter")
            ),
            pid=typ.cast("int | None", options.get("pid")),
            observation=typ.cast(
                "_StageObservation | None", options.get("observation")
            ),
        )
    if context.capture:
        try:
            await (context.eof_grace_waiter or _await_eof_grace)(consumers)
        except asyncio.CancelledError:
            with contextlib.suppress(asyncio.CancelledError):
                await _settle_consumers(consumers)
            raise
        pending_count = sum(not task.done() for task in consumers)
        if pending_count:
            _DRAIN_LOGGER.debug(
                "capture_eof_grace_expired pending_readers=%s",
                pending_count,
                extra={
                    "cuprum_pending_readers": pending_count,
                    "cuprum_timeout_s": _CAPTURE_EOF_GRACE_S,
                },
            )
            _report_capture_eof_grace_expiry(
                context.observation,
                pid=context.pid,
                eof_grace_s=_CAPTURE_EOF_GRACE_S,
                pending_readers=pending_count,
            )
    stdout_result, stderr_result = await _settle_consumers(consumers)
    drain_errors = tuple(
        type(result).__name__
        for result in (stdout_result, stderr_result)
        if isinstance(result, BaseException)
        and not isinstance(result, asyncio.CancelledError)
    )
    if drain_errors:
        _report_teardown_drain_failure(
            context.observation, pid=context.pid, error_types=drain_errors
        )
    for stream, result in zip(
        ("stdout", "stderr"), (stdout_result, stderr_result), strict=True
    ):
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
    stdout_text = _decode_consumer_result(stdout_result, capture=context.capture)
    stderr_text = _decode_consumer_result(stderr_result, capture=context.capture)
    return stdout_text, stderr_text


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
) -> list[str | BaseException | None]:
    """Cancel unfinished consumers and drain every result once."""
    _cancel_pending_consumers(consumers)
    return await asyncio.gather(*consumers, return_exceptions=True)


async def _wait_for_exit_code_within_timeout(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
) -> tuple[int, float]:
    """Await the exit code under ``execution.timeout``, or unbounded when ``None``.

    A non-positive timeout denotes an already-elapsed deadline. ``asyncio.timeout``
    would only schedule its cancellation for the next event-loop iteration, so a
    fast, already-exited process whose ``wait()`` never suspends would race past
    it and return successfully. To keep ``run(timeout=0)`` deterministic — and to
    preserve the behaviour of the ``asyncio.wait_for`` implementation this
    replaced — a non-positive deadline expires immediately: the deadline wait
    is skipped, so the process is never given the chance to exit on its own,
    but terminating it still awaits its actual exit before
    :class:`TimeoutError` is raised.

    Stream consumers belong to the caller, which drains them exactly once via
    :func:`_drain_stream_consumers`; terminating the process here lets those
    consumers reach EOF during that drain.

    Both expiry routes emit a structured ``cuprum.timeout`` log record and a
    best-effort ``timeout`` observe event tagged with the timeout mode
    (``"non_positive_immediate"`` versus ``"elapsed_deadline"``) before the
    :class:`TimeoutError` propagates; both are best-effort and cannot mask the
    timeout.

    Returns
    -------
    tuple[int, float]
        The process exit code and the ``perf_counter`` timestamp of exit,
        as produced by :func:`_wait_for_exit_code`.

    Raises
    ------
    TimeoutError
        If ``execution.timeout`` is non-positive, denoting an
        already-elapsed deadline; the process is terminated first.
    """
    timeout = execution.timeout
    if timeout is not None and timeout <= 0:
        # Shielded for the same reason as the cancellation branch above: a
        # caller cancelling here would otherwise skip the reap.
        await _terminate_all_shielded((process,), execution.ctx.cancel_grace)
        _report_timeout_expiry(
            execution.observation,
            pid=process.pid,
            configured_timeout=timeout,
            mode="non_positive_immediate",
        )
        raise TimeoutError
    try:
        async with asyncio.timeout(timeout):
            return await _wait_for_exit_code(process, execution.ctx)
    except TimeoutError as exc:
        # Reached only on asyncio.timeout expiry (a positive deadline elapsed);
        # _wait_for_exit_code has already terminated the process, and the caller
        # drains the stream consumers exactly once.
        _report_timeout_expiry(
            execution.observation,
            pid=process.pid,
            configured_timeout=_require_timeout(timeout, exc),
            mode="elapsed_deadline",
        )
        raise


async def _reconcile_run_tasks(
    tasks: _RunTaskOwnership,
    context: _DrainContext,
) -> tuple[str | None, str | None]:
    """Cancel the stdin writer and drain the stream consumers, in that order.

    The two halves are one unit so a caller can run them under
    :func:`_shielded_cleanup` and know both finish: draining first would leave
    a writer blocked on a pipe nobody is reading, and shielding them separately
    would let a cancellation landing between the two strand the consumers.

    Returns
    -------
    tuple[str | None, str | None]
        The decoded stdout and stderr text, as produced by
        :func:`_drain_stream_consumers`.
    """
    await _cancel_stdin_writer(tasks.stdin_task)
    return await _drain_stream_consumers(
        tasks.consumers,
        context,
    )


__all__ = [
    "_DrainContext",
    "_RunTaskOwnership",
    "_await_eof_grace",
    "_cancel_pending_consumers",
    "_decode_consumer_result",
    "_drain_stream_consumers",
    "_reconcile_run_tasks",
    "_settle_consumers",
    "_wait_for_exit_code",
    "_wait_for_exit_code_within_timeout",
]
