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

Teardown routes the termination through the run's *owns_group* flag, so a
caller that omits it silently reduces an owning run to the direct-child signal
and lets the group survive. Requiring the keyword makes an omitted ownership
value a ``TypeError`` at the call site instead of a lost guarantee, and every
caller that targets the child alone must say so.
"""

from __future__ import annotations

import asyncio
import time
import typing as typ

from cuprum._capture_cleanup import _finalize_capture_handles
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
from cuprum._teardown_policy import _TeardownPolicy
from cuprum._timeout_reporting import _report_timeout_expiry

if typ.TYPE_CHECKING:
    from cuprum._pipeline_types import _StageObservation
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_wait_types import (
        _DrainPair,
        _RunTaskOwnership,
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
#
# The drain's own log records are emitted under the historical channel name
# ``"cuprum._subprocess_wait"``, spelled explicitly in ``_stream_drain``, so
# moving the code did not move the channel operators filter on.


async def _wait_for_exit_code(
    process: asyncio.subprocess.Process,
    ctx: ExecutionContext,
    *,
    owns_group: bool,
    observation: _StageObservation | None = None,
) -> tuple[int, float]:
    """Wait for a subprocess exit code, terminating it on expiry or cancel.

    Waiting for the exit code is this helper's sole responsibility. Any stream
    consumers belong to the caller, which drains them exactly once when the wait
    fails (see :func:`_run_subprocess_with_streams`); terminating the process
    here lets those consumers reach EOF during that drain.

    *observation* is the run being waited on, carried through to that teardown
    so a group it could not reach publishes the ``group_teardown`` event
    against the execution it belongs to. One process is waited on, so one
    execution is named; the default is ``None`` for the callers with no
    execution in hand, which keep the log record alone.

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
        # ``_Wait4Process`` holds its pipes even once it exits, so the group
        # teardown is what lets a descendant release the drain this wait is
        # unwinding from.
        await _terminate_all_shielded(
            (process,),
            _TeardownPolicy(ctx.cancel_grace, owns_group=owns_group),
            observations=(observation,) if observation is not None else (),
        )
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
    :func:`cuprum._stream_drain._drain_stream_consumers`; terminating the
    process here lets those consumers reach EOF during that drain.

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
        await _terminate_all_shielded(
            (process,),
            _TeardownPolicy(
                execution.ctx.cancel_grace,
                owns_group=execution.owns_process_group,
            ),
            observations=(execution.observation,),
        )
        _report_timeout_expiry(
            execution.observation,
            pid=process.pid,
            configured_timeout=timeout,
            mode="non_positive_immediate",
        )
        raise TimeoutError
    try:
        async with asyncio.timeout(timeout):
            return await _wait_for_exit_code(
                process,
                execution.ctx,
                owns_group=execution.owns_process_group,
                observation=execution.observation,
            )
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


def _deadline_for_collection(execution: _SubprocessExecution) -> float | None:
    """Return the seconds left for the post-exit boundary, or ``None`` unbounded.

    The awaits this bounds are reached only once the exit wait has returned, so
    a run whose deadline was already consumed has no budget left for them. It is
    a remainder from the same reference the exit wait was scheduled against, and
    it is measured where it is used rather than earlier on purpose: a duration
    computed before the awaits it bounds would let them spend part of the budget
    and still hand the whole of it to what follows. The caller passes this
    straight to one ``asyncio.timeout`` covering all three awaits a streamed run
    has left after its child exited. Returning ``None`` for an unconfigured
    timeout keeps a run without a deadline unbounded here, exactly as it is
    everywhere else.

    The clock is the execution's own ``monotonic_clock`` rather than a direct
    read, so a test can hold the deadline still and assert the remainder the
    boundary is actually given instead of racing a real one. It is the same
    callable the run's ``started_at`` was sampled from, which is what makes the
    subtraction a duration rather than a difference between two clocks.

    It lives here, beside the exit wait, because the remainder is only
    meaningful against the reference that wait was scheduled from — splitting
    the two would leave the subtraction with no way to state which start it is
    measured from.

    Returns
    -------
    float | None
        The seconds remaining before the run's deadline, possibly negative if
        the exit wait overran it, or ``None`` when no timeout is configured.
    """
    if execution.timeout is None:
        return None
    elapsed = execution.observation.monotonic_clock() - execution.started_at
    return execution.timeout - elapsed


def _drain_context(
    execution: _SubprocessExecution,
    tasks: _RunTaskOwnership,
    pid: int | None,
    *,
    preserve: bool,
) -> _DrainContext:
    """Build the drain context for one reconciliation of *tasks*.

    Every reconciliation route a streamed run has needs the same four values,
    and the only one they disagree about is whether output is preserved: the
    expiry routes keep the text their ``TimeoutExpired`` will report, while a
    route re-raising some other error discards it rather than spending the
    capture grace on text nobody will read. Building the bundle at each site
    instead is how a route drifts from the others, so the one value that
    legitimately differs is the one asked for here. A discarding route reports
    ``(None, None)``, the pair that says no stream is being captured at all.

    It sits beside :func:`_reconcile_run_tasks`, its only consumer, because the
    context exists to serve that one reconciliation and reads its cancellation
    policy from the tasks it is given.

    ``capture_bytes`` is passed through from the execution rather than
    defaulted, so an absent reader under a byte-exact run is handed ``b""``
    and never the text-mode ``""``.

    Returns
    -------
    _DrainContext
        The context that reconciliation, and the drain beneath it, read their
        capture choice, pid, observation, and cancellation policy from.

    Examples
    --------
    A discarding route reports no policies at all, which is how the drain knows
    to skip the capture grace::

        context = _drain_context(execution, tasks, pid, preserve=False)
    """
    return _DrainContext(
        captures=(
            (execution.capture_stdout, execution.capture_stderr)
            if preserve
            else (None, None)
        ),
        capture_bytes=execution.capture_bytes,
        pid=pid,
        observation=execution.observation,
        discard_on_cancel=tasks.discard_on_cancel,
    )


async def _reconcile_run_tasks(
    tasks: _RunTaskOwnership,
    context: _DrainContext,
) -> _DrainPair:
    """Stop the idle heartbeat, cancel the stdin writer, then drain the streams.

    The stream consumers drain with ``return_exceptions=True``, so their
    already-recorded diagnostics survive the cancellation that a teardown
    performs: a cancelled reader keeps the fallback it recorded before it was
    cancelled.

    Every half of the teardown is one unit so a caller can run them under
    :func:`_shielded_cleanup` and know all of them finish: draining first would
    leave a writer blocked on a pipe nobody is reading, and shielding them
    separately would let a cancellation landing between two of them strand the
    rest. The accumulator cleanup below belongs to that unit for the same
    reason, and it is placed here rather than inside the drain because a
    *drain-local* ``finally`` would not run when the drain task is cancelled
    mid-await — which is precisely the case a spill file has to survive.

    The heartbeat goes first. Reconciliation runs once the run is already
    ending, and a keepalive announcing that a terminated child is "still
    running" is worse than no keepalive at all. Stopping is idempotent, so the
    run's other exit paths can call it too.

    Returns
    -------
    _DrainPair
        The stdout and stderr results, as produced by
        :func:`_drain_stream_consumers`.
    """
    await _stop_idle_monitor(tasks.idle)
    await _cancel_stdin_writer(tasks.stdin_task)
    stdout_drain, stderr_drain = await _drain_stream_consumers(
        tasks.consumers,
        context,
    )
    # After the drain, not before: a reader still running is still appending,
    # and closing the file underneath it would turn the tail of a spilled
    # capture into a storage failure that the run then has to report.
    await _finalize_capture_handles((stdout_drain, stderr_drain), tasks.captures)
    for diagnostics in tasks.relay_diagnostics:
        diagnostics.settle()
    return stdout_drain, stderr_drain


__all__ = [
    "_CAPTURE_EOF_GRACE_S",
    "_DrainContext",
    "_await_eof_grace",
    "_deadline_for_collection",
    "_drain_context",
    "_drain_stream_consumers",
    "_reconcile_run_tasks",
    "_wait_for_exit_code",
    "_wait_for_exit_code_within_timeout",
]
