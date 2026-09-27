"""Waiting for a child's exit, and the deadline that bounds that wait.

The rules for *ending* a run divide in three, and this module owns the first
part: how long to wait for a child to exit, what a deadline expiry does to the
child, and which exception a cancelled wait raises.
``cuprum._subprocess_wait`` owns the reconciliation that follows — cancelling
the stdin writer and settling the run's consumer tasks — and
``cuprum._stream_drain`` owns the drain rules that reconciliation runs.

The separation is not arbitrary. Everything here is about the *child*: it
awaits a process, terminates it when the wait cannot finish, and translates
the result into the public timeout surface. Nothing here touches the parent's
stream tasks or the run's observability, which is what keeps a reader wedged on
a pipe from being able to delay the termination that lets it reach EOF.

A deadline expiry and an external cancellation both arrive as
:class:`asyncio.CancelledError`, and both need identical teardown: terminate
the child so the caller's drain can reach EOF, then re-raise. Termination goes
through ``_terminate_all_shielded`` rather than ``_terminate_process``
directly, so a caller cancelling during the grace period cannot skip the
``SIGKILL`` escalation and strand a child.
"""

from __future__ import annotations

import asyncio
import time
import typing as typ

from cuprum._process_exit import _await_process_exit
from cuprum._process_lifecycle import _terminate_all_shielded
from cuprum._subprocess_timeout import _require_timeout
from cuprum._teardown_policy import _TeardownPolicy
from cuprum._timeout_reporting import _report_timeout_expiry

if typ.TYPE_CHECKING:
    from cuprum._pipeline_types import _StageObservation
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.sh import ExecutionContext


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
    fails (see :func:`~cuprum._subprocess_wait._reconcile_run_tasks`);
    terminating the process here lets those consumers reach EOF during that
    drain.

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
    :func:`cuprum._subprocess_wait._reconcile_run_tasks`; terminating the
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


__all__ = [
    "_deadline_for_collection",
    "_wait_for_exit_code",
    "_wait_for_exit_code_within_timeout",
]
