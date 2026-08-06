"""Shared scaffolding for the `_pipeline_wait` completion-ordering tests.

The completion transition is exercised from four angles — a Hypothesis state
machine, pinned examples, the async boundary, and the structured records — each
in its own module. The setup they share lives here so a change to how a wait
state is built, or to how termination is intercepted, lands in one place rather
than four.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import logging
import types
import typing as typ

from cuprum import ECHO, _pipeline_wait, sh
from cuprum._pipeline_types import _ExecutionHooks, _StageObservation
from cuprum._pipeline_wait import _PipelineWaitState

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

    from cuprum.events import ExecHook


async def immediate(exit_code: int) -> int:
    """Return ``exit_code`` after a yield, standing in for ``Process.wait()``."""
    await asyncio.sleep(0)
    return exit_code


async def _still_running() -> int:
    """Never complete, standing in for a stage that has not yet exited.

    The fail-fast path asks the wait tasks which stages are still running, so a
    stage whose completion has not been applied has to read as pending rather
    than be missing from the list.
    """
    never: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    return await never


def stage_exec_id(stage_index: int) -> str:
    """Return the deterministic correlation token this scaffolding uses.

    Real tokens are UUIDs minted per stage. These stand in for them so records
    can be asserted on and snapshotted exactly, while still proving each stage
    reports its *own* token rather than a shared or hard-coded one.
    """
    return f"exec-{stage_index}"


def make_stage_observations(
    stage_count: int,
    hooks: tuple[ExecHook, ...],
) -> tuple[_StageObservation, ...]:
    """Build one observation per stage, each publishing to ``hooks``.

    Mirrors what ``_build_pipeline_observations`` produces for a real
    pipeline — a distinct command, its position in the tags, and a freshly
    minted token per stage — without spawning anything, so a fail-fast event
    can be driven one completion at a time.
    """
    builder = sh.make(ECHO)
    execution_hooks = _ExecutionHooks(
        before_hooks=(),
        after_hooks=(),
        observe_hooks=hooks,
    )
    return tuple(
        _StageObservation(
            cmd=builder("stage", str(idx)),
            hooks=execution_hooks,
            tags=types.MappingProxyType(
                {
                    "project": "pipeline-wait-tests",
                    "pipeline_stage_index": idx,
                    "pipeline_stages": stage_count,
                },
            ),
            cwd=None,
            env_overlay=None,
            pending_tasks=[],
        )
        for idx in range(stage_count)
    )


def make_wait_state(
    stage_count: int,
    *,
    observations: tuple[_StageObservation, ...] = (),
) -> _PipelineWaitState:
    """Build a bare wait state for ``stage_count`` stages.

    The pure ``record_completion`` transition touches only the exit-code,
    timing, and failure-index bookkeeping, so the task fields are left empty:
    no event loop or subprocess is required to exercise completion ordering.
    Correlation tokens are supplied because the records the runtime layer
    emits do read them.

    When ``observations`` is given the tokens are derived from it exactly as
    ``cuprum._pipeline_internals`` derives them, so a test can assert that the
    token on the fail-fast event is the same one the log records carry rather
    than two independently stubbed values that happen to agree.
    """
    exec_ids = (
        tuple(str(obs.exec_id) for obs in observations)
        if observations
        else tuple(stage_exec_id(idx) for idx in range(stage_count))
    )
    return _PipelineWaitState(
        wait_tasks=[],
        task_to_index={},
        exit_codes=[None] * stage_count,
        started_at=[0.0] * stage_count,
        ended_at=[None] * stage_count,
        exec_ids=exec_ids,
        observations=observations,
    )


def record_terminations(
    monkeypatch: pytest.MonkeyPatch,
    *,
    terminated_count: int = 0,
) -> list[tuple[int, float]]:
    """Intercept fail-fast termination, returning the list it records into.

    Each entry is the ``(failure_index, cancel_grace)`` the production code
    asked for. Recording rather than signalling keeps these tests free of real
    processes while still proving termination was requested — and requested
    exactly once per fail-fast, which a silent stub could not show.

    ``terminated_count`` is what the stub reports back as the number of stages
    it stopped, standing in for the count the real helper derives from the
    still-running stages.
    """
    terminations: list[tuple[int, float]] = []

    async def fake_terminate(
        processes: object,
        wait_tasks: object,
        failure_index: int,
        *,
        cancel_grace: float,
    ) -> int:
        """Record the termination request instead of signalling processes."""
        del processes, wait_tasks
        await asyncio.sleep(0)
        terminations.append((failure_index, cancel_grace))
        return terminated_count

    monkeypatch.setattr(
        _pipeline_wait,
        "_terminate_pipeline_remaining_stages",
        fake_terminate,
    )
    return terminations


def pin_clock(monkeypatch: pytest.MonkeyPatch, value: float) -> None:
    """Freeze the wait module's clock so emitted durations are deterministic.

    ``_pipeline_wait`` binds ``perf_counter`` as a module attribute of its own,
    so this replaces that binding rather than ``time.perf_counter``. The
    difference matters: patching the stdlib attribute would pin the clock for
    every module in the process for the duration of the test.
    """
    monkeypatch.setattr(_pipeline_wait, "perf_counter", lambda: value)


@dc.dataclass(slots=True)
class AdvancingClock:
    """A settable stand-in for the wait module's monotonic clock."""

    reading: float = 0.0

    def __call__(self) -> float:
        """Return the current reading, as ``perf_counter`` would."""
        return self.reading


def advancing_clock(monkeypatch: pytest.MonkeyPatch) -> AdvancingClock:
    """Replace the wait module's clock with one the test moves by hand.

    Assigning ``reading`` before each completion is what makes the stages'
    recorded end times distinguishable, where `pin_clock` would give them all
    one value. It advances per completion rather than per reading because a
    fail-fast completion also times its own teardown, and those reads must not
    shift the next stage's recorded end time.

    Patches the same narrow seam as `pin_clock`, for the same reason.
    """
    clock = AdvancingClock()
    monkeypatch.setattr(_pipeline_wait, "perf_counter", clock)
    return clock


def structured_fields(record: logging.LogRecord) -> dict[str, object]:
    """Return a record's ``cuprum_``-prefixed structured fields.

    ``extra=`` sets these directly on the record instance, so they are not
    attributes of ``LogRecord`` itself; read them from the instance dictionary
    the way ``cuprum.adapters.logging_adapter`` selects its own fields.
    """
    return {
        key: value for key, value in vars(record).items() if key.startswith("cuprum_")
    }


def field_values(
    records: cabc.Sequence[logging.LogRecord],
    key: str,
) -> list[object]:
    """Return ``key`` from each record that carries it, in record order.

    Selecting one structured field across a run of records is what nearly
    every assertion in these modules does, whichever field it is about. This
    is the one selector for all of them: records that do not carry ``key`` are
    skipped rather than reported as ``None``, so an assertion on the values can
    tell "no record carried it" from "a record carried a null".
    """
    return [
        fields[key]
        for fields in (structured_fields(record) for record in records)
        if key in fields
    ]


def record_actions(records: cabc.Sequence[logging.LogRecord]) -> list[str]:
    """Return the ``cuprum_action`` field of each pipeline-wait record."""
    return [str(value) for value in field_values(records, "cuprum_action")]


@dc.dataclass(frozen=True, slots=True)
class CompletionPlan:
    """One pipeline's worth of completions to drive through the boundary."""

    stage_count: int
    completions: list[tuple[int, int]]
    # What the intercepted termination reports back as the stages it stopped.
    terminated_count: int = 0
    # Supplied only by tests that assert on the fail-fast observe event; the
    # log records need no observation context.
    observations: tuple[_StageObservation, ...] = ()


def apply_completions(
    state: _PipelineWaitState,
    completions: list[tuple[int, int]],
    *,
    before_each: cabc.Callable[[int], None] | None = None,
) -> None:
    """Apply each ``(stage_index, exit_code)`` through the async boundary.

    The state is given one wait task per stage, the way `_wait_for_pipeline`
    builds it, and a stage's task is swapped for a settled one only when that
    stage's completion is applied. Stages whose completions have not been
    applied therefore read as still running — which is what the fail-fast path
    consults to decide there is anything left to terminate. A single-task
    stand-in would make every sibling look already settled and suppress the
    teardown these tests are about.

    ``before_each`` is called with the zero-based step index just before each
    completion, for tests that move the clock between them.

    Split out from `drive_completions` so a test that needs its own
    termination stub, or that expects the boundary to raise, can drive the
    same completions without `drive_completions` installing a second stub over
    the top of it.
    """

    async def drive() -> None:
        """Apply each completion through ``_process_completed_task``."""
        standins = [asyncio.create_task(_still_running()) for _ in state.exit_codes]
        state.wait_tasks = list(standins)
        state.task_to_index = {task: idx for idx, task in enumerate(standins)}
        try:
            for step, (idx, exit_code) in enumerate(completions):
                if before_each is not None:
                    before_each(step)
                task = asyncio.create_task(immediate(exit_code))
                await task
                state.wait_tasks[idx] = task
                state.task_to_index[task] = idx
                await _pipeline_wait._process_completed_task(task, state, [], 0.25)
        finally:
            # Runs on the raising path too, so a hook failure leaves no
            # stand-in behind for the loop to complain about on shutdown.
            for standin in standins:
                standin.cancel()
            await asyncio.gather(*standins, return_exceptions=True)

    asyncio.run(drive())


def drive_completions(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    plan: CompletionPlan,
) -> list[logging.LogRecord]:
    """Drive ``plan`` through the real async boundary, capturing logs.

    ``plan.completions`` is applied in order, one ``(stage_index, exit_code)``
    pair at a time, to a state built for ``plan.stage_count`` stages;
    ``plan.terminated_count`` is what the intercepted termination reports back
    as the number of stages it stopped. ``started_at`` is zero for every stage
    and the clock is pinned to 12.5, so an emitted stage duration is always
    exactly 12.5.
    """
    terminations = record_terminations(
        monkeypatch,
        terminated_count=plan.terminated_count,
    )
    pin_clock(monkeypatch, 12.5)

    state = make_wait_state(plan.stage_count, observations=plan.observations)

    with caplog.at_level(logging.WARNING, logger=_pipeline_wait.__name__):
        apply_completions(state, plan.completions)

    # Termination bookkeeping must stay consistent with the records.
    expected = record_actions(caplog.records).count("pipeline_fail_fast_termination")
    assert len(terminations) == expected, (  # noqa: S101 - test scaffolding assertion
        "each fail-fast termination record must accompany exactly one termination call"
    )
    return caplog.records
