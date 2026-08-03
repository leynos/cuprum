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
    import pytest

    from cuprum.events import ExecHook


async def immediate(exit_code: int) -> int:
    """Return ``exit_code`` after a yield, standing in for ``Process.wait()``."""
    await asyncio.sleep(0)
    return exit_code


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
    """Freeze ``time.perf_counter`` so emitted durations are deterministic."""
    monkeypatch.setattr(_pipeline_wait.time, "perf_counter", lambda: value)


def structured_fields(record: logging.LogRecord) -> dict[str, object]:
    """Return a record's ``cuprum_``-prefixed structured fields.

    ``extra=`` sets these directly on the record instance, so they are not
    attributes of ``LogRecord`` itself; read them from the instance dictionary
    the way ``cuprum.adapters.logging_adapter`` selects its own fields.
    """
    return {
        key: value for key, value in vars(record).items() if key.startswith("cuprum_")
    }


def record_actions(records: list[logging.LogRecord]) -> list[str]:
    """Return the ``cuprum_action`` field of each pipeline-wait record."""
    return [
        str(fields["cuprum_action"])
        for fields in (structured_fields(record) for record in records)
        if "cuprum_action" in fields
    ]


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
) -> None:
    """Apply each ``(stage_index, exit_code)`` through the async boundary.

    Split out from `drive_completions` so a test that needs its own
    termination stub, or that expects the boundary to raise, can drive the
    same completions without `drive_completions` installing a second stub over
    the top of it.
    """

    async def drive() -> None:
        """Apply each completion through ``_process_completed_task``."""
        for idx, exit_code in completions:
            task = asyncio.create_task(immediate(exit_code))
            await task
            state.wait_tasks = [task]
            state.task_to_index = {task: idx}
            await _pipeline_wait._process_completed_task(task, state, [], 0.25)

    asyncio.run(drive())


def drive_completions(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    plan: CompletionPlan,
) -> list[logging.LogRecord]:
    """Drive ``completions`` through the real async boundary, capturing logs.

    Each entry is a ``(stage_index, exit_code)`` pair applied in order.
    ``started_at`` is zero for every stage and the clock is pinned to 12.5, so
    an emitted stage duration is always exactly 12.5.
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
