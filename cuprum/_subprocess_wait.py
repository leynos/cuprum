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

from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._process_exit import _await_process_exit
from cuprum._process_lifecycle import _terminate_all_shielded
from cuprum._stream_drain import (
    _CAPTURE_EOF_GRACE_S,
    _await_eof_grace,
    _drain_stream_consumers,
)
from cuprum._subprocess_stdin import _cancel_stdin_writer
from cuprum._subprocess_timeout import _require_timeout
from cuprum._subprocess_wait_types import _DrainContext
from cuprum._timeout_reporting import _report_timeout_expiry

if typ.TYPE_CHECKING:
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_wait_types import (
        _RunTaskOwnership,
        _StreamPayload,
    )
    from cuprum.sh import ExecutionContext


# ``_CAPTURE_EOF_GRACE_S``, ``_await_eof_grace``, and ``_DrainContext`` are
# imported rather than defined here, but they stay bound in this module's
# namespace deliberately: the drain once lived here, and callers still import
# them by this path. Bouncing them through keeps one definition while leaving
# those imports working.
#
# Importing them from here is supported; patching them here is not. The drain
# resolves each name from its own module's globals, so rebinding the copy in
# this namespace is invisible to it. Tests that need to replace the grace
# waiter must patch ``cuprum._stream_drain._await_eof_grace``.


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
) -> tuple[_StreamPayload | None, _StreamPayload | None]:
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
    tuple[_StreamPayload | None, _StreamPayload | None]
        The stdout and stderr payloads, as produced by
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
    "_CAPTURE_EOF_GRACE_S",
    "_DrainContext",
    "_await_eof_grace",
    "_drain_stream_consumers",
    "_reconcile_run_tasks",
    "_wait_for_exit_code",
    "_wait_for_exit_code_within_timeout",
]
