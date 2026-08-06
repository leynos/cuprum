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
import time
import typing as typ

from cuprum._process_lifecycle import _terminate_all_shielded
from cuprum._subprocess_stdin import _cancel_stdin_writer
from cuprum._subprocess_timeout import _require_timeout
from cuprum._timeout_reporting import (
    _report_teardown_drain_failure,
    _report_timeout_expiry,
)

if typ.TYPE_CHECKING:
    from cuprum._pipeline_types import _StageObservation
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.sh import ExecutionContext


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
    """
    try:
        exit_code = await process.wait()
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
    *,
    pid: int | None = None,
    observation: _StageObservation | None = None,
) -> tuple[str | None, str | None]:
    """Cancel pending consumers, drain them once, and decode their output.

    A consumer that failed or was cancelled maps to ``None`` so a broken reader
    cannot mask the surrounding failure. Draining here exactly once keeps the
    timeout and cancellation paths from reconciling the same tasks twice.

    A consumer that drains with an unexpected exception (anything other than the
    ``CancelledError`` produced by cancelling it) is still absorbed to preserve
    the primary timeout or cancellation, but is reported through
    :func:`_report_teardown_drain_failure` — a structured log record plus, when
    ``observation`` is supplied, a best-effort ``teardown_error`` observe event
    — so the drain failure stays observable.
    """
    _cancel_pending_consumers(consumers)
    stdout_result, stderr_result = await asyncio.gather(
        *consumers, return_exceptions=True
    )
    drain_errors = tuple(
        type(result).__name__
        for result in (stdout_result, stderr_result)
        if isinstance(result, BaseException)
        and not isinstance(result, asyncio.CancelledError)
    )
    if drain_errors:
        _report_teardown_drain_failure(observation, pid=pid, error_types=drain_errors)
    stdout_text = None if isinstance(stdout_result, BaseException) else stdout_result
    stderr_text = None if isinstance(stderr_result, BaseException) else stderr_result
    return stdout_text, stderr_text


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
    stdin_task: asyncio.Task[None] | None,
    consumers: tuple[asyncio.Task[str | None], asyncio.Task[str | None]],
    *,
    pid: int | None,
    observation: _StageObservation | None = None,
) -> tuple[str | None, str | None]:
    """Cancel the stdin writer and drain the stream consumers, in that order.

    The two halves are one unit so a caller can run them under
    :func:`_shielded_cleanup` and know both finish: draining first would leave
    a writer blocked on a pipe nobody is reading, and shielding them separately
    would let a cancellation landing between the two strand the consumers.
    """
    await _cancel_stdin_writer(stdin_task)
    return await _drain_stream_consumers(consumers, pid=pid, observation=observation)


__all__ = [
    "_cancel_pending_consumers",
    "_drain_stream_consumers",
    "_reconcile_run_tasks",
    "_wait_for_exit_code",
    "_wait_for_exit_code_within_timeout",
]
