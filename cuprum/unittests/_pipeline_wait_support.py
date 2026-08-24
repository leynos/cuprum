"""Shared scaffolding for the `_pipeline_wait` completion-ordering tests.

The completion transition is exercised from four angles — a Hypothesis state
machine, pinned examples, the async boundary, and fail-fast events — each in
its own module. The setup they share lives here so a change to how a wait state
is built, or to how termination is intercepted, lands in one place rather than
four.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
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


def make_stage_observations(
    stage_count: int,
    hooks: tuple[ExecHook, ...],
    *,
    tag_overrides: cabc.Mapping[str, object] | None = None,
) -> tuple[_StageObservation, ...]:
    """Build one observation per stage, each publishing to ``hooks``.

    Mirrors what ``_build_pipeline_observations`` produces for a real
    pipeline — a distinct command, its position in the tags, and a freshly
    minted token per stage — without spawning anything, so a fail-fast event
    can be driven one completion at a time.

    ``tag_overrides`` stands in for the caller's ``ExecutionContext.tags``, and
    is merged last for the same reason the real builder merges them last: a
    caller may shadow ``pipeline_stage_index``. Supplying it is how a test
    pulls the tag apart from the index the coordinator acted on.
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
                    **(tag_overrides or {}),
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
    When ``observations`` is given, the runtime report reads its correlation
    token directly from the matching stage rather than retaining a duplicate
    tuple for every pipeline.
    """
    return _PipelineWaitState(
        wait_tasks=[],
        task_to_index={},
        exit_codes=[None] * stage_count,
        started_at=[0.0] * stage_count,
        ended_at=[None] * stage_count,
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

    Tests install their own termination stub before calling this helper, so
    each can inspect the requested stage index and count independently.
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
