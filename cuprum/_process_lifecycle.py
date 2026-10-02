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
however many cancellations arrive before re-raising.

The pipeline waiter decides when fail-fast teardown is necessary; this module
owns the subprocess handles and executes that decision alongside timeout and
error cleanup. Starting that pipeline — and cleaning up the resources left by
a partial spawn — belongs to ``cuprum._pipeline_spawn``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import typing as typ

from cuprum._pipeline_stream_results import _reconcile_pipe_tasks
from cuprum._process_exit import _await_process_exit
from cuprum.context import current_context
from cuprum.context._policy import _resolve_env_policy
from cuprum.context.env_overlay import EnvMode, EnvOverlay, render_env

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _signal_child(
    process: asyncio.subprocess.Process,
    *,
    group_signal: int,
    direct: cabc.Callable[[], None],
    owns_group: bool,
) -> None:
    """Signal the child alone, or the whole group when this run owns it.

    *owns_group* is only true for a child spawned with
    ``start_new_session=True``, which makes that child the leader of its own
    session and process group. The group is therefore named by the child's own
    process identifier: no ``os.getpgid`` lookup is needed, and no other
    process can be in a group this run addresses but did not create.

    Signalling the group rather than the direct child is what lets a child's
    descendants reach end-of-file on inherited pipes; a descendant that left
    the group deliberately is outside this call's reach, exactly as
    :class:`~cuprum.sh.ProcessGroupPolicy` documents.

    *direct* is the child-only route — ``process.terminate`` or
    ``process.kill`` — passed in rather than selected here so a run that does
    not own the group signals exactly the process it always did. Both
    ``os.killpg`` and those two raise ``ProcessLookupError`` when the target
    has already gone, so callers keep one handler across either route.
    """
    if owns_group and process.pid is not None:
        os.killpg(process.pid, group_signal)
        return
    direct()


async def _terminate_process(
    process: asyncio.subprocess.Process,
    grace_period: float,
    *,
    owns_group: bool = False,
) -> None:
    """Terminate a running process, escalating to kill after the grace period."""
    await _terminate_process_with_wait(
        process,
        grace_period=grace_period,
        is_done=lambda: process.returncode is not None,
        wait_for_exit=lambda: _await_process_exit(process),
        owns_group=owns_group,
    )


async def _terminate_process_with_wait(
    process: asyncio.subprocess.Process,
    *,
    grace_period: float,
    is_done: cabc.Callable[[], bool],
    wait_for_exit: cabc.Callable[[], cabc.Awaitable[int]],
    owns_group: bool = False,
) -> bool:
    """Terminate a process and report whether its waiter completed.

    The two-phase grace is unchanged whether the run owns the child's group or
    not: the first phase asks every member of the target to exit, the second
    compels whichever of them outlived the grace period. Only the target of
    those signals differs, and :func:`_signal_child` is the one place that
    decides it.
    """
    grace_period = max(0.0, grace_period)
    if is_done():
        return False
    try:
        _signal_child(
            process,
            group_signal=signal.SIGTERM,
            direct=process.terminate,
            owns_group=owns_group,
        )
    except (ProcessLookupError, OSError):
        return False
    try:
        await asyncio.wait_for(wait_for_exit(), grace_period)
    except asyncio.TimeoutError:  # ruff: ignore[timeout-error-alias] - explicit asyncio timeout needed
        try:
            _signal_child(
                process,
                group_signal=signal.SIGKILL,
                direct=process.kill,
                owns_group=owns_group,
            )
        except (ProcessLookupError, OSError):
            return False
        await wait_for_exit()
    return True


async def _shielded_cleanup[T](cleanup: cabc.Awaitable[T]) -> T:
    """Complete cleanup before re-raising any caller cancellation."""
    task = asyncio.ensure_future(cleanup)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)
        if not task.cancelled():
            # Retrieve the outcome so a cleanup failure is not reported as an
            # unretrieved task exception. The caller's cancellation still wins:
            # it is the error the run is ending on.
            task.exception()
        raise


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
    *,
    owns_group: cabc.Sequence[bool] | bool = False,
) -> None:
    """Terminate every process before re-raising caller cancellation.

    *owns_group* accepts either one flag for every process, the single-command
    case, or one flag per process in ``processes`` order, the pipeline case
    where stages may have been spawned under different policies. A short or
    absent sequence leaves the remaining processes on the direct-child route
    rather than guessing, so an ownership bookkeeping slip degrades to the
    previous behaviour instead of signalling a group this run does not own.
    """
    await _await_teardown_shielded(
        _terminate_process(
            process,
            cancel_grace,
            owns_group=_owns_group_at(owns_group, index),
        )
        for index, process in enumerate(processes)
    )


def _owns_group_at(owns_group: cabc.Sequence[bool] | bool, index: int) -> bool:
    """Return the ownership flag for the process at *index*."""
    if isinstance(owns_group, bool):
        return owns_group
    return index < len(owns_group) and owns_group[index]


async def _cleanup_pipeline_on_error(
    processes: list[asyncio.subprocess.Process],
    pipe_tasks: list[asyncio.Task[None]],
    cancel_grace: float,
    *,
    owns_group: cabc.Sequence[bool] | bool = False,
) -> list[object]:
    """Clean up pipeline resources after an error or cancellation."""
    # Terminate every process, then cancel and collect the pipe tasks owned by
    # the caller. This delivers cancellation to a native pump before waiting
    # for it to return descriptor ownership. Stream consumer tasks remain
    # owned by the caller (``_run_pipeline``), not by this helper.
    await _terminate_all_shielded(processes, cancel_grace, owns_group=owns_group)
    return await _reconcile_pipe_tasks(pipe_tasks)


def _merge_env(
    extra: EnvOverlay | None,
    env_mode: EnvMode = EnvMode.OVERLAY,
    *,
    include_context_overlay: bool = True,
) -> dict[str, str] | None:
    """Render the ambient and per-call environment policies for a child."""
    context = current_context()
    parent_overlay = context.env_overlay if include_context_overlay else None
    parent_mode = context.env_mode if include_context_overlay else EnvMode.OVERLAY
    overlay, mode = _resolve_env_policy(
        parent_overlay,
        parent_mode,
        extra,
        env_mode,
    )
    return render_env(overlay, mode)


async def _terminate_process_via_wait_task(
    process: asyncio.subprocess.Process,
    wait_task: asyncio.Task[int],
    grace_period: float,
    *,
    owns_group: bool = False,
) -> bool:
    """Terminate a process and report whether its provided waiter completed."""
    return await _terminate_process_with_wait(
        process,
        grace_period=grace_period,
        is_done=wait_task.done,
        wait_for_exit=lambda: asyncio.shield(wait_task),
        owns_group=owns_group,
    )


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
    *,
    owns_group: cabc.Sequence[bool] | bool = False,
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
    await _terminate_all_shielded(processes, cancel_grace, owns_group=owns_group)


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
    owns_group: cabc.Sequence[bool] | bool = False,
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
            _terminate_process_via_wait_task(
                process,
                wait_task,
                cancel_grace,
                owns_group=_owns_group_at(owns_group, idx),
            ),
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
