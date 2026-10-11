"""Which execution a teardown's refusal is filed against.

A fail-fast teardown signals several stages, and a refusal it meets on one of
them must be reported against *that* stage. The report is the only place the
refusal is visible: the caller receives the same ``False`` whether a stage had
already settled or could not be signalled at all, and the count the fail-fast
path publishes treats both as zero. A refusal filed against the wrong stage
therefore shows a clean run for the stage that failed and an unexplained
failure for a stage that settled normally.

The pairing is easy to lose because the two halves travel separately: the loop
signals a process, while the report needs the execution that process belongs
to. Holding them as one row is what these tests pin, and the correlation token
is how they observe it. ``exec_id`` rather than the pid is the witness because
the pid is read off the process being signalled — it was already right when the
attribution was wrong — and because this event deliberately carries no stage
index, so the token is the only field that names the execution.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import itertools
import typing as typ

from cuprum._process_lifecycle import (
    _StageHandle,
    _terminate_all_shielded,
    _terminate_pipeline_remaining_stages,
)
from cuprum._teardown_policy import _TeardownPolicy
from cuprum.events import GroupTeardownOutcome
from cuprum.unittests._pipeline_wait_support import (
    _still_running,
    immediate,
    make_stage_observations,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import _StageObservation
    from cuprum.events import ExecEvent

# Short, because no case here reaches the grace period: a refused signal
# returns from the teardown before the escalation is considered.
_GRACE = 0.05

_GROUP_TEARDOWN = "group_teardown"


class _StageProcess:
    """A stage handle that records which signals reached it.

    Shared by both doubles below so a refusal and a settled exit differ only in
    what ``terminate`` does, which is the single fact under test.
    """

    def __init__(self, pid: int, *, refuses: bool) -> None:
        """Record the pid and how this stage answers the graceful signal."""
        self.pid = pid
        self.refuses = refuses
        self.returncode: int | None = None
        self.escalations = 0
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        """Await the exit ``terminate`` or ``kill`` records."""
        await self._exited.wait()
        return self.returncode if self.returncode is not None else 0

    def terminate(self) -> None:
        """Accept the graceful signal, or refuse it as the kernel would.

        Raises
        ------
        OSError
            When this stage refuses. The wider ``OSError`` is deliberate:
            ``ProcessLookupError`` is the race teardown tolerates silently —
            same ``False``, no report — so only the wider refusal produces the
            events these tests read. ``ProcessLookupError`` and
            ``PermissionError`` are both ``OSError`` subclasses, and the wrapper
            this reaches tells them apart, so the type raised here is what
            selects the refusal route.
        """
        if self.refuses:
            msg = "Operation not permitted"
            raise OSError(1, msg)
        self.returncode = -15
        self._exited.set()

    def kill(self) -> None:
        """Record the escalation and exit.

        A refusal returns from the teardown before this is reached, so a
        non-zero count here would mean the refusal had been swallowed and the
        teardown had signalled a target it never reached — a different fault
        from the one under test, and one these tests would otherwise report as a
        missing event.
        """
        self.escalations += 1
        self.returncode = -9
        self._exited.set()


def _refusing(pid: int) -> _StageProcess:
    """Return a stage whose graceful signal the kernel refuses."""
    return _StageProcess(pid, refuses=True)


def _settling(pid: int) -> _StageProcess:
    """Return a stage that exits on the graceful signal, as most do."""
    return _StageProcess(pid, refuses=False)


def _handle(
    observation: _StageObservation,
    process: _StageProcess,
    wait_task: asyncio.Task[int],
) -> _StageHandle:
    """Pair one stage's process, waiter, and execution into one teardown row."""
    return _StageHandle(
        process=typ.cast("asyncio.subprocess.Process", process),
        wait_task=wait_task,
        observation=observation,
    )


def _rows(
    observations: cabc.Sequence[_StageObservation],
    processes: cabc.Sequence[_StageProcess],
    wait_tasks: cabc.Sequence[asyncio.Task[int]],
) -> list[_StageHandle]:
    """Zip observations, processes, and waiters into stage rows."""
    return list(
        itertools.starmap(
            _handle,
            zip(observations, processes, wait_tasks, strict=True),
        )
    )


def _refusals(events: cabc.Sequence[ExecEvent]) -> list[ExecEvent]:
    """Return the group-teardown events, in the order they were emitted."""
    return [event for event in events if event.phase == _GROUP_TEARDOWN]


@dc.dataclass(frozen=True, slots=True)
class _DrivenCase:
    """One driven teardown and everything it published.

    Grouped so the assertions outside the async scope read the same values the
    driving coroutine did, rather than a second run's worth of fresh tokens.
    """

    events: tuple[ExecEvent, ...]
    processes: tuple[_StageProcess, ...]
    outcomes: tuple[bool, ...]


async def _drop_tasks(*tasks: asyncio.Task[int]) -> None:
    """Cancel the stand-in waiters so the loop reports none left behind."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _exited_stages(*codes: int) -> list[asyncio.Task[int]]:
    """Return settled wait tasks carrying ``codes``, as exited stages produce."""
    tasks = [asyncio.create_task(immediate(code)) for code in codes]
    await asyncio.gather(*tasks)
    return tasks


def _assert_refused_against(event: ExecEvent, observation: _StageObservation) -> None:
    """Assert ``event`` is this observation's refusal, and is a refusal at all.

    The fields beside the token are asserted too, so an unrelated event leaking
    through the phase filter cannot satisfy the attribution check: only a
    refused signal carries this outcome, and only this route files it under the
    graceful signal's operation.
    """
    assert event.exec_id == observation.exec_id, (
        "the refusal must carry the execution whose process was signalled; "
        f"got {event.exec_id!r}, expected {observation.exec_id!r}"
    )
    assert event.group_outcome is GroupTeardownOutcome.SIGNAL_REFUSED, (
        f"the event must be a refusal; got {event.group_outcome!r}"
    )
    assert event.operation == "teardown_signal_15", (
        f"the refusal must name the refusing phase; got {event.operation!r}"
    )
    assert event.owns_group is False, (
        "an inheriting run reports the direct route it actually took; "
        f"got {event.owns_group!r}"
    )
    assert event.settle_s is None, (
        "no wait was measured on this route, so an absent figure must not be "
        f"published as an instant one; got {event.settle_s!r}"
    )


def test_a_later_stage_refusal_is_filed_against_that_stage() -> None:
    """The stage signalled is the stage reported, not the first in the pipeline.

    Stage zero has already exited and stage one is the failure, so the teardown
    has exactly one target: stage two. That is the shape the misattribution
    needed — a run-level sequence of observations reports against whichever
    execution sits at the front, and here the front is a stage that was never
    signalled. The stage *selection* is sound either way, which is why the pid
    alone cannot witness this: the pid is read off the process that was
    signalled, so it was already correct when the token beside it was wrong.
    """

    async def run_case() -> _DrivenCase:
        """Signal the surviving stage and collect what that reported."""
        events: list[ExecEvent] = []
        observations = make_stage_observations(3, (events.append,))
        tokens = {observation.exec_id for observation in observations}
        # Without this, an implementation filing every report against one stage
        # would still satisfy a check for "some stage's token".
        assert len(tokens) == 3, "each stage must mint its own token"

        settled = await _exited_stages(0, 1)
        running = asyncio.create_task(_still_running())
        processes = [_refusing(8201), _refusing(8202), _refusing(8203)]
        stages = _rows(observations, processes, [*settled, running])
        try:
            outcomes = await _terminate_pipeline_remaining_stages(
                stages,
                _TeardownPolicy(_GRACE),
                # Stage one failed; stage zero's exit is merely why the first
                # selected target is not at the front.
                1,
            )
        finally:
            await _drop_tasks(running)
        refusals = _refusals(events)
        # Asserted inside the driven scope so the token read here is the one
        # this run minted, not a second set built for the assertion.
        assert len(refusals) == 1, (
            "exactly one target was signalled, so exactly one refusal may be "
            f"reported; got {[(event.pid, event.exec_id) for event in events]}"
        )
        _assert_refused_against(refusals[0], observations[2])
        return _DrivenCase(
            events=tuple(events),
            processes=tuple(processes),
            outcomes=outcomes,
        )

    driven = asyncio.run(run_case())

    assert driven.outcomes == (False,), (
        "the refused signal is the one selected target's outcome, reported as "
        f"nothing done; got {driven.outcomes}"
    )
    assert _refusals(driven.events)[0].pid == 8203, (
        "the report must name the process that was signalled; "
        f"got {_refusals(driven.events)[0].pid}"
    )
    assert [process.escalations for process in driven.processes] == [0, 0, 0], (
        "a refusal returns from the teardown rather than escalating; "
        f"got {[process.escalations for process in driven.processes]}"
    )


def test_two_refused_stages_are_reported_against_two_executions() -> None:
    """Two targets, two refusals, two distinct executions.

    With stage zero the failure and stages one and two still running, the
    teardown signals both. A pipeline that pairs its observations positionally
    reports the *same* execution twice here, so this case fails for every stage
    rather than only for the one that happens to sit behind an exited sibling.
    """

    async def run_case() -> list[ExecEvent]:
        """Refuse both targets and collect the two reports."""
        events: list[ExecEvent] = []
        observations = make_stage_observations(3, (events.append,))
        settled = await _exited_stages(7)
        running = [
            asyncio.create_task(_still_running()),
            asyncio.create_task(_still_running()),
        ]
        stages = _rows(
            observations,
            [_refusing(8301), _refusing(8302), _refusing(8303)],
            [*settled, *running],
        )
        try:
            await _terminate_pipeline_remaining_stages(
                stages,
                _TeardownPolicy(_GRACE),
                0,
            )
        finally:
            await _drop_tasks(*running)
        return events

    refusals = _refusals(asyncio.run(run_case()))

    assert [event.pid for event in refusals] == [8302, 8303], (
        "each still-running stage must be signalled once, in stage order; "
        f"got {[event.pid for event in refusals]}"
    )
    tokens = [event.exec_id for event in refusals]
    assert len(set(tokens)) == 2, (
        "two stages were signalled, so the two reports must name two "
        f"executions rather than one repeated; got {tokens}"
    )


def test_a_settled_stage_is_not_reported_at_all() -> None:
    """The positive counterpart: a signal that arrives reports nothing.

    Stage one accepts the graceful signal and stage two refuses it. Both are
    targets of the same teardown, so the difference between them is only
    whether the signal arrived — which is what makes this a control rather than
    a restatement of the case above. Without it, a report emitted for every
    target would satisfy the attribution assertions while making an ordinary
    teardown indistinguishable from a failed one.
    """

    async def run_case() -> tuple[list[ExecEvent], tuple[bool, ...]]:
        """Settle one target, refuse the other, and collect what was said."""
        events: list[ExecEvent] = []
        observations = make_stage_observations(3, (events.append,))
        settled = await _exited_stages(3)
        settling = _settling(8402)
        settling_waiter = asyncio.create_task(settling.wait())
        running = asyncio.create_task(_still_running())
        stages = _rows(
            observations,
            [_refusing(8401), settling, _refusing(8403)],
            [settled[0], settling_waiter, running],
        )
        try:
            outcomes = await _terminate_pipeline_remaining_stages(
                stages,
                # Long enough that the settling target completes its own wait
                # rather than running out the grace period.
                _TeardownPolicy(1.0),
                0,
            )
        finally:
            await _drop_tasks(settling_waiter, running)
        return events, outcomes

    events, outcomes = asyncio.run(run_case())

    assert outcomes == (True, False), (
        "the settling target is a completed teardown and the refusing one is "
        f"not; got {outcomes}"
    )
    assert [event.pid for event in _refusals(events)] == [8403], (
        "only the target whose signal was refused may be reported; got "
        f"{[(event.pid, event.exec_id) for event in events]}"
    )


def test_a_run_with_no_execution_to_name_reports_no_event() -> None:
    """An absent observation is not replaced by one the caller never named.

    The spawn-failure and abandoned-run routes reach teardown with no execution
    in hand. Inventing one would file the refusal against an unrelated run, so
    the outcome stays on the log record — which is why the two channels travel
    together rather than the event replacing the record. The hook here is
    non-empty and the routes above prove it delivers, so the silence is the
    absent identity and not a hook that never fires.
    """

    async def run_case() -> tuple[list[ExecEvent], list[_StageProcess]]:
        """Tear down two refusing processes with no observations at all."""
        events: list[ExecEvent] = []
        observations = make_stage_observations(2, (events.append,))
        assert observations[0].hooks.observe_hooks, (
            "the collector must be a live hook, or the silence below is unfalsifiable"
        )
        processes = [_refusing(8501), _refusing(8502)]
        with contextlib.suppress(Exception):
            await _terminate_all_shielded(
                [
                    typ.cast("asyncio.subprocess.Process", process)
                    for process in processes
                ],
                _TeardownPolicy(_GRACE),
            )
        return events, processes

    events, processes = asyncio.run(run_case())

    assert [process.escalations for process in processes] == [0, 0], (
        "a refusal returns from the teardown rather than escalating; "
        f"got {[process.escalations for process in processes]}"
    )
    assert _refusals(events) == [], (
        "a route with no execution to correlate with must not invent one; "
        f"got {[(event.pid, event.exec_id) for event in events]}"
    )
