"""Process termination and the shared cancellation-safe cleanup primitive.

Termination sends SIGTERM, waits out a grace period, then escalates to
SIGKILL and reaps the exit, whether for one process
(``_terminate_process``) or a whole pipeline
(``_cleanup_pipeline_on_error``, ``_terminate_timed_out_stages``,
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
a partial spawn — belongs to ``cuprum._pipeline_spawn``. What a teardown
signal is delivered to, and what a completed teardown waits for, belongs to
``cuprum._process_signal``: this module decides *when* to signal, that one
decides *what* the signal reaches.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import typing as typ

from cuprum._pipeline_stream_results import _reconcile_pipe_tasks
from cuprum._process_exit import _await_process_exit
from cuprum._process_signal import _SettlementTarget, _terminate_process_with_wait
from cuprum.context import current_context
from cuprum.context._policy import _resolve_env_policy
from cuprum.context.env_overlay import EnvMode, EnvOverlay, render_env

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import _StageObservation
    from cuprum._teardown_policy import _TeardownPolicy


async def _terminate_process(
    process: asyncio.subprocess.Process,
    policy: _TeardownPolicy,
    *,
    observation: _StageObservation | None = None,
) -> None:
    """Terminate a running process, escalating to kill after the grace period."""
    await _terminate_process_with_wait(
        process,
        policy=policy,
        target=_SettlementTarget(
            is_done=lambda: process.returncode is not None,
            wait_for_exit=lambda: _await_process_exit(process),
        ),
        observation=observation,
    )


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
    policy: _TeardownPolicy,
    *,
    observations: cabc.Sequence[_StageObservation] = (),
) -> None:
    """Terminate every process before re-raising caller cancellation.

    *policy* carries the per-process ownership flags, so each process is
    signalled the way it was spawned. A policy whose sequence is shorter than
    ``processes`` leaves the rest on the direct-child route.

    *observations* carries the executions these processes belong to, in the
    same order, so each termination is reported against the execution whose
    process it signalled. Pairing is by index and the sequence is not passed
    on whole: a run-level report drawn from the front of the sequence would
    attribute every stage's refusal to the first execution, which is a
    different run from the one that was signalled. The default is empty for
    the routes with no execution to correlate with; those keep the log record
    alone.
    """
    await _await_teardown_shielded(
        _terminate_process(
            process,
            policy.for_index(index),
            observation=observations[index] if index < len(observations) else None,
        )
        for index, process in enumerate(processes)
    )


async def _cleanup_pipeline_on_error(
    processes: list[asyncio.subprocess.Process],
    pipe_tasks: list[asyncio.Task[None]],
    policy: _TeardownPolicy,
    *,
    observations: cabc.Sequence[_StageObservation] = (),
) -> list[object]:
    """Clean up pipeline resources after an error or cancellation."""
    # Terminate every process, then cancel and collect the pipe tasks owned by
    # the caller. This delivers cancellation to a native pump before waiting
    # for it to return descriptor ownership. Stream consumer tasks remain
    # owned by the caller (``_run_pipeline``), not by this helper.
    await _terminate_all_shielded(processes, policy, observations=observations)
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


@dc.dataclass(frozen=True, slots=True)
class _StageHandle:
    """One pipeline stage, as teardown addresses it.

    A stage is only ever terminated through its process and its waiter
    together: the process is what gets signalled and the waiter is what says
    whether it has settled. Held as one row rather than as two parallel lists
    so a stage's two halves cannot be paired with the wrong stage by an
    enumeration that walks them separately.

    The stage's own *observation* is a member for the same reason. A teardown
    reports against the execution it signalled, so the pairing of process to
    observation is part of what the row means; carrying the observations as a
    separate sequence would let a loop index one list while signalling from
    the other, which is how a refusal for stage two came to be filed against
    stage zero.
    """

    process: asyncio.subprocess.Process
    wait_task: asyncio.Task[int]
    observation: _StageObservation | None = None


async def _terminate_process_via_wait_task(
    stage: _StageHandle,
    policy: _TeardownPolicy,
) -> bool:
    """Terminate a stage and report whether its provided waiter completed."""
    return await _terminate_process_with_wait(
        stage.process,
        policy=policy,
        target=_SettlementTarget(
            is_done=stage.wait_task.done,
            wait_for_exit=lambda: asyncio.shield(stage.wait_task),
        ),
        observation=stage.observation,
    )


def _stage_may_own_live_group(
    index: int,
    owns_group: cabc.Sequence[bool],
) -> bool:
    """Return whether stage ``index`` owns a group this selection must reach."""
    return index < len(owns_group) and owns_group[index]


def _stages_to_terminate(
    failure_index: int,
    done: cabc.Sequence[bool],
    *,
    owns_group: cabc.Sequence[bool] = (),
) -> list[int]:
    """Return the stage indices to terminate after a fail-fast, each once.

    This is the pure selection behind ``_terminate_pipeline_remaining_stages``.
    Every stage is scheduled at most once — indices come from a single
    enumeration — and the two exclusion rules turn on ownership:

    - A stage that does *not* own its group is selected exactly when it is
      neither the failed stage nor already finished. The failed stage owns its
      own exit, and a finished stage has no direct child left to signal.
    - A stage that *does* own its group is always selected, whatever its wait
      task says. Its group outlives the leader that named it, so a completed
      waiter is not settlement: a descendant the stage left behind can still
      hold an inherited pipe. Whether anything is actually signalled is decided
      later, by the group probe in
      :func:`cuprum._process_signal._settlement_of`, which answers for the
      group rather than for the leader.

    ``owns_group`` is per stage, so a short or absent sequence leaves the
    remaining stages on the direct-child rule: an ownership bookkeeping slip
    degrades to the selection every run had before ownership existed, rather
    than signalling a group this run never created.

    Cleanup is idempotent for the inherited route — a second pass over settled
    stages selects nothing — while an owned stage stays selectable by design,
    because the group probe, not this selection, is what reports it settled.

    Returns
    -------
    list[int]
        The stage indices to terminate, in ascending stage order.
    """
    return [
        idx
        for idx, is_done in enumerate(done)
        if _stage_may_own_live_group(idx, owns_group)
        or (idx != failure_index and not is_done)
    ]


async def _terminate_timed_out_stages(
    processes: cabc.Sequence[asyncio.subprocess.Process],
    policy: _TeardownPolicy,
    *,
    observations: cabc.Sequence[_StageObservation] = (),
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
    await _terminate_all_shielded(processes, policy, observations=observations)


def _has_stages_to_terminate(
    failure_index: int,
    wait_tasks: cabc.Sequence[asyncio.Task[int]],
    policy: _TeardownPolicy,
) -> bool:
    """Report whether a fail-fast at ``failure_index`` has anything to stop.

    Asks the same reducer `_terminate_pipeline_remaining_stages` picks its
    targets with, so a caller that announces the decision before requesting the
    teardown cannot disagree with it about whether a stage was left running.

    *policy* supplies the per-stage ownership the reducer selects on: an owned
    stage is a termination target even when its wait task has completed, since
    its group can outlive the leader that named it.

    Returns
    -------
    bool
        Whether at least one stage remains to terminate.
    """
    return bool(
        _stages_to_terminate(
            failure_index,
            [wait_task.done() for wait_task in wait_tasks],
            owns_group=policy.ownership_of(len(wait_tasks)),
        ),
    )


async def _terminate_pipeline_remaining_stages(
    stages: cabc.Sequence[_StageHandle],
    policy: _TeardownPolicy,
    failure_index: int,
) -> tuple[bool, ...]:
    """Terminate all still-running stages after a stage fails.

    Once a stage exits non-zero, Cuprum applies fail-fast semantics by
    terminating the remaining pipeline stages. This prevents pipelines from
    hanging on long-running producers/consumers when downstream work is no
    longer meaningful.

    Each stage is terminated through its own :class:`_StageHandle`, so a
    refusal is reported against the execution whose process was signalled.
    The selection is ascending but need not start at stage zero — when the
    first stage has already exited and a later one fails, the first target is
    the next stage still running — so an observation drawn from the front of
    a run-level sequence would name a different execution from the one
    signalled.

    Returns
    -------
    tuple[bool, ...]
        One outcome for each selected termination target. ``True`` means the
        process accepted termination and its waiter completed; ``False`` means
        it had already settled or could not be signalled.
    """
    targets = _stages_to_terminate(
        failure_index,
        [stage.wait_task.done() for stage in stages],
        owns_group=policy.ownership_of(len(stages)),
    )
    known = set(targets)
    termination_tasks = [
        asyncio.create_task(
            _terminate_process_via_wait_task(stage, policy.for_index(idx)),
        )
        for idx, stage in enumerate(stages)
        if idx in known
    ]
    if not termination_tasks:
        return ()
    outcomes = await _await_teardown_shielded(termination_tasks)
    return tuple(outcome is True for outcome in outcomes)
