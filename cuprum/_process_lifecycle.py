"""Process termination and the shared cancellation-safe cleanup primitive.

Termination sends SIGTERM, waits out a grace period, then escalates to
SIGKILL and reaps the exit, whether for one process
(``_terminate_process``, ``_terminate_process_with_wait``) or a whole
pipeline (``_cleanup_pipeline_on_error``, ``_terminate_timed_out_stages``,
``_terminate_pipeline_remaining_stages``). ``_shielded_cleanup`` underlies all
of that: it is the cancellation-safe primitive shared by the pipeline paths
(``_pipeline_internals``, ``_pipeline_collect``) and the single-command
subprocess paths (``_subprocess_execution``, ``_subprocess_wait``,
``_command_internals._execute_with_hooks``). A bare ``asyncio.shield`` is not
enough on its own: it stops cancellation reaching the inner coroutine, but
the awaiting coroutine resumes immediately, so a caller's ``CancelledError``
propagates while cleanup is still running. ``_shielded_cleanup`` instead
retries the wait under a shield until the owned task is done, absorbing
however many cancellations arrive before re-raising. ``_run_shielded_step``
refines that primitive for callers that must run *two* cleanups and decide
afterwards which failure the caller is owed: it reports how the step ended,
distinguishing a cancellation from an ordinary failure, so the deciding stays
with the caller rather than being duplicated into every cleanup.

The pipeline waiter decides when fail-fast teardown is necessary; this module
owns the subprocess handles and executes that decision alongside timeout and
error cleanup. Starting that pipeline — and cleaning up the resources left by
a partial spawn — belongs to ``cuprum._pipeline_spawn``.
"""

from __future__ import annotations

import asyncio
import contextlib
import typing as typ

from cuprum._pipeline_stream_results import _reconcile_pipe_tasks
from cuprum._process_exit import _await_process_exit

if typ.TYPE_CHECKING:
    import collections.abc as cabc


async def _terminate_process(
    process: asyncio.subprocess.Process,
    grace_period: float,
) -> _TerminationOutcome:
    """Terminate a running process, escalating to kill after the grace period."""
    return await _terminate_process_with_wait(
        process,
        grace_period=grace_period,
        is_done=lambda: process.returncode is not None,
        wait_for_exit=lambda: _await_process_exit(process),
    )


class _TerminationOutcome(typ.NamedTuple):
    """What a cooperative stop did to the child, and how the wait for it ended.

    Three facts that a single ``bool`` used to flatten, kept apart because they
    answer different questions and can disagree. ``delivered`` is about the
    child: whether any signal was actually sent to it. ``escalated`` is about
    the grace window: whether it closed with the child still running. And
    ``waiter_completed`` is about the caller's own waiter: whether it returned
    rather than being left pending.

    They disagree at the edges. A forced signal that raises
    ``ProcessLookupError`` — the child died in the instant between the window
    closing and the kill — leaves ``delivered`` and ``escalated`` both true
    while ``waiter_completed`` is false, because the SIGTERM that started the
    window did reach a child, and the waiter that was watching it may still be
    the caller's to await. Reporting one ``bool`` here would have to pick one
    of those questions and silently answer the others wrong.

    ``refused`` is the fourth fact and the only one this module never sets. The
    stop helpers cannot tell a delivery the platform would not make from a
    child that had already gone, because both reach them as the same
    ``ProcessLookupError``; only the child's own status separates them, and the
    handle reads that. It defaults to ``False`` so the helpers keep reporting
    exactly what they observed, and the one caller that can weigh the two
    against each other rewrites it.
    """

    delivered: bool
    escalated: bool
    waiter_completed: bool
    refused: bool = False


_NO_STOP = _TerminationOutcome(
    delivered=False,
    escalated=False,
    waiter_completed=False,
)
"""What a stop reports when it sent nothing to the child.

Both of this module's early returns mean the same thing — the child was not
running, or the signal never reached it — and neither the grace window nor the
waiter was ever engaged.
"""

_TERMINATED_IN_GRACE = _TerminationOutcome(
    delivered=True,
    escalated=False,
    waiter_completed=True,
)
"""The cooperative ending: the child took the graceful signal and exited in time."""

_KILLED_AFTER_GRACE = _TerminationOutcome(
    delivered=True,
    escalated=True,
    waiter_completed=True,
)
"""The escalated ending: the grace window ran out with the child alive."""

_KILLED_AFTER_GRACE_UNWAITED = _TerminationOutcome(
    delivered=True,
    escalated=True,
    waiter_completed=False,
)
"""The escalated ending whose reaping wait never returned.

The graceful signal reached the child and the window did run out, but the
forced signal's own ``ProcessLookupError`` means the exit this caller would
have waited for was collected elsewhere — see the class docstring for why the
three facts disagree here.
"""


async def _terminate_process_with_wait(
    process: asyncio.subprocess.Process,
    *,
    grace_period: float,
    is_done: cabc.Callable[[], bool],
    wait_for_exit: cabc.Callable[[], cabc.Awaitable[int]],
) -> _TerminationOutcome:
    """Terminate a process and report what the stop did to it."""
    grace_period = max(0.0, grace_period)
    if is_done():
        return _NO_STOP
    try:
        process.terminate()
    except (ProcessLookupError, OSError):
        return _NO_STOP
    # Past this point the graceful signal reached the child, whatever happens
    # to the waiter below.
    try:
        await asyncio.wait_for(wait_for_exit(), grace_period)
    except asyncio.TimeoutError:  # ruff: ignore[timeout-error-alias] - explicit asyncio timeout needed
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            return _KILLED_AFTER_GRACE_UNWAITED
        await wait_for_exit()
        return _KILLED_AFTER_GRACE
    return _TERMINATED_IN_GRACE


async def _settle_shielded[T](task: asyncio.Task[T]) -> T | None:
    """Wait out a shielded task after cancellation; report what it produced.

    Retrieving the outcome here keeps a task failure from surfacing later as an
    unretrieved task exception.

    Returns
    -------
    T | None
        What the task produced, or ``None`` when it produced nothing.
    """
    while not task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)
    if task.cancelled() or task.exception() is not None:
        return None
    return task.result()


async def _shielded_cleanup[T](cleanup: cabc.Awaitable[T]) -> T:
    """Complete cleanup before re-raising any caller cancellation."""
    task = asyncio.ensure_future(cleanup)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _settle_shielded(task)
        raise


class _ShieldedStepFailure[T](typ.NamedTuple):
    """How one shielded cleanup step ended, for the caller to weigh.

    A cancellation is reported separately from an ordinary failure because the
    two are owed different treatment: a cancellation must always reach the
    caller — it is a request that the task stop, not a failure to report — while
    an ordinary failure may legitimately defer to an error the caller is
    already raising. Reporting both in one value keeps that distinction visible
    to the caller instead of flattening it here.

    The value is carried alongside the failure rather than only returned on
    success, because a step's *outcome* and its *completion* are independent
    facts. A stop that delivered what it was asked to and then had its caller
    cancelled has still stopped the child, and a caller describing that stop
    needs to say so; a report that dropped the value whenever anything went
    wrong would leave it unable to.
    """

    cancellation: asyncio.CancelledError | None
    failure: BaseException | None
    value: T | None


async def _run_shielded_step[T](
    cleanup: cabc.Awaitable[T],
) -> _ShieldedStepFailure[T]:
    """Run a shielded cleanup, reporting rather than raising how it ended.

    The step is shielded exactly as ``_shielded_cleanup`` shields it, so a
    cancellation arriving mid-step still completes it. What differs is only
    what happens to the outcome: it is returned, because a caller running two
    steps has to weigh the first against the second before it can say which
    failure, if any, the caller is owed. Absorbing the failure here would
    decide that too early; raising it here would abandon the second step.

    Returns
    -------
    _ShieldedStepFailure
        How the step ended — the cancellation it absorbed, the failure it
        raised, or neither — together with whatever the step produced. The
        value is absent only when the step failed or was cancelled.
    """
    task = asyncio.ensure_future(cleanup)
    try:
        value = await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        # The step's own value survives the cancellation rather than being
        # dropped: a stop that delivered its signal and was cancelled while the
        # delivery was finishing has still stopped the child, and the control
        # event describing it has to be emitted.
        return _ShieldedStepFailure(cancelled, None, await _settle_shielded(task))
    except BaseException as failed:  # ruff: ignore[blind-except] - reported, not handled: the caller weighs it against a later step's failure
        return _ShieldedStepFailure(None, failed, None)
    return _ShieldedStepFailure(None, None, value)


async def _await_teardown_shielded(
    teardowns: cabc.Iterable[cabc.Awaitable[object]],
) -> tuple[object, ...]:
    """Complete teardown and return its outcomes after caller cancellation."""
    outcomes = await _shielded_cleanup(
        asyncio.gather(*teardowns, return_exceptions=True)
    )
    return tuple(outcomes)


async def _terminate_all_shielded(
    processes: cabc.Iterable[asyncio.subprocess.Process],
    cancel_grace: float,
) -> None:
    """Terminate every process before re-raising caller cancellation."""
    await _await_teardown_shielded(
        _terminate_process(process, cancel_grace) for process in processes
    )


async def _cleanup_pipeline_on_error(
    processes: list[asyncio.subprocess.Process],
    pipe_tasks: list[asyncio.Task[None]],
    cancel_grace: float,
) -> list[object]:
    """Clean up pipeline resources after an error or cancellation."""
    # Terminate every process, then cancel and collect the pipe tasks owned by
    # the caller. This delivers cancellation to a native pump before waiting
    # for it to return descriptor ownership. Stream consumer tasks remain
    # owned by the caller (``_run_pipeline``), not by this helper.
    await _terminate_all_shielded(processes, cancel_grace)
    return await _reconcile_pipe_tasks(pipe_tasks)


async def _terminate_process_via_wait_task(
    process: asyncio.subprocess.Process,
    wait_task: asyncio.Task[int],
    grace_period: float,
) -> bool:
    """Terminate a process and report whether its provided waiter completed."""
    outcome = await _terminate_process_with_wait(
        process,
        grace_period=grace_period,
        is_done=wait_task.done,
        wait_for_exit=lambda: asyncio.shield(wait_task),
    )
    return outcome.waiter_completed


def _stages_to_terminate(
    failure_index: int,
    done: cabc.Sequence[bool],
) -> list[int]:
    """Return the stage indices to terminate after a fail-fast, each once."""
    # This is the pure selection behind _terminate_pipeline_remaining_stages.
    # Every stage is scheduled at most once — indices come from a single
    # enumeration — and two stages are never scheduled: the failed stage, which
    # owns its own exit, and any already-finished stage, which needs no
    # termination. Cleanup is therefore idempotent: a second pass over settled
    # stages (all ``done``) selects nothing.
    return [
        idx for idx, is_done in enumerate(done) if idx != failure_index and not is_done
    ]


async def _terminate_timed_out_stages(
    processes: cabc.Sequence[asyncio.subprocess.Process],
    cancel_grace: float,
) -> None:
    """Terminate every still-running stage after a pipeline deadline expires.

    ``_wait_for_pipeline`` normally performs this teardown itself when the
    deadline cancels it. A non-positive deadline gives ``asyncio.wait_for`` a
    zero timeout, and it then cancels that coroutine before it ever runs, so
    nothing terminates the stages and they are left orphaned for the rest of
    their natural life. Terminating here covers that route.

    Running on both routes is safe and deliberate: ``_terminate_process``
    returns immediately for a stage that has already exited. Doing it before
    the output gather also lets the stage pipes reach EOF, so a captured
    pipeline cannot block waiting on a producer that is still running.

    Failures are absorbed — this runs while a timeout is already propagating,
    and must not replace the ``TimeoutExpired`` the caller awaits.
    """
    await _terminate_all_shielded(processes, cancel_grace)


def _has_stages_to_terminate(
    failure_index: int,
    wait_tasks: cabc.Sequence[asyncio.Task[int]],
) -> bool:
    """Report whether a fail-fast at ``failure_index`` has anything to stop.

    Asks the same reducer `_terminate_pipeline_remaining_stages` picks its
    targets with, so a caller that announces the decision before requesting the
    teardown cannot disagree with it about whether a stage was left running.

    Returns
    -------
    bool
        Whether at least one stage remains to terminate.
    """
    return bool(
        _stages_to_terminate(
            failure_index,
            [wait_task.done() for wait_task in wait_tasks],
        ),
    )


async def _terminate_pipeline_remaining_stages(
    processes: list[asyncio.subprocess.Process],
    wait_tasks: list[asyncio.Task[int]],
    failure_index: int,
    *,
    cancel_grace: float,
) -> tuple[bool, ...]:
    """Terminate all still-running stages after a stage fails.

    Once a stage exits non-zero, Cuprum applies fail-fast semantics by
    terminating the remaining pipeline stages. This prevents pipelines from
    hanging on long-running producers/consumers when downstream work is no
    longer meaningful.

    Returns
    -------
    tuple[bool, ...]
        One outcome for each selected termination target. ``True`` means the
        process accepted termination and its waiter completed; ``False`` means
        it had already settled or could not be signalled.
    """
    targets = set(
        _stages_to_terminate(
            failure_index,
            [wait_task.done() for wait_task in wait_tasks],
        )
    )
    termination_tasks = [
        asyncio.create_task(
            _terminate_process_via_wait_task(process, wait_task, cancel_grace),
        )
        for idx, (process, wait_task) in enumerate(
            zip(processes, wait_tasks, strict=True),
        )
        if idx in targets
    ]
    if not termination_tasks:
        return ()
    outcomes = await _await_teardown_shielded(termination_tasks)
    return tuple(outcome is True for outcome in outcomes)
