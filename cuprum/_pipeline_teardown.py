"""Pipeline teardown: which stages a failure stops, and how.

Split from ``cuprum._process_lifecycle`` so that module stays about the
termination primitive itself — ``_terminate_process`` and the cancellation-safe
``_shielded_cleanup`` that every run path shares — while this one holds the
pipeline's own selection and the loop around it. The seam was already there:
what a teardown signal is delivered to belongs to ``cuprum._process_signal``,
when to signal a single process stays with ``_process_lifecycle``, and which
stages a fail-fast reaches is what moved.

The selection is a pure reducer, :func:`_stages_to_terminate`, deliberately
separate from the loop that acts on it. A caller announcing the fail-fast
decision before requesting the teardown asks the same reducer the teardown
does, so the announcement and the act cannot disagree about whether a stage
was left running. Ownership is threaded through that reducer per stage rather
than as one run-level flag, because an owned stage is a target even when its
waiter has completed: its group outlives the leader that named it.

A stage is addressed through :class:`_StageHandle`, which pairs a process with
its waiter *and* its observation. The pairing is the point: each teardown
reports against the execution it signalled, so the three cannot be walked as
parallel sequences that a loop index could disagree about.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum._pipeline_stream_results import _reconcile_pipe_tasks
from cuprum._process_lifecycle import (
    _await_teardown_shielded,
    _terminate_all_shielded,
    _terminate_process_with_wait,
)
from cuprum._process_signal import _SettlementTarget

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import _StageObservation
    from cuprum._teardown_policy import _TeardownPolicy


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
    outcome = await _terminate_process_with_wait(
        stage.process,
        policy=policy,
        target=_SettlementTarget(
            is_done=stage.wait_task.done,
            wait_for_exit=lambda: asyncio.shield(stage.wait_task),
        ),
        observation=stage.observation,
    )
    return outcome.waiter_completed


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
        One outcome for each selected termination target, reporting that
        target's
        :attr:`~cuprum._process_lifecycle._TerminationOutcome.waiter_completed`
        — whether a reaping wait returned rather than being left pending.
        ``True`` therefore covers a process that took the graceful signal and
        one that ignored it until it was killed; ``False`` covers a process
        that had already settled, one no signal could reach, and one whose
        escalation was refused.
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
