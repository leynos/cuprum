"""Shared scaffolding for the `_pipeline_wait` completion-ordering tests.

The completion transition is exercised from four angles — a Hypothesis state
machine, pinned examples, the async boundary, and the structured records — each
in its own module. The setup they share lives here so a change to how a wait
state is built, or to how termination is intercepted, lands in one place rather
than four.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum import _pipeline_wait
from cuprum._pipeline_wait import _PipelineWaitState

if typ.TYPE_CHECKING:
    import pytest


async def immediate(exit_code: int) -> int:
    """Return ``exit_code`` after a yield, standing in for ``Process.wait()``."""
    await asyncio.sleep(0)
    return exit_code


def make_wait_state(stage_count: int) -> _PipelineWaitState:
    """Build a bare wait state for ``stage_count`` stages.

    The pure ``record_completion`` transition touches only the exit-code,
    timing, and failure-index bookkeeping, so the task fields are left empty:
    no event loop or subprocess is required to exercise completion ordering.
    """
    return _PipelineWaitState(
        wait_tasks=[],
        task_to_index={},
        exit_codes=[None] * stage_count,
        started_at=[0.0] * stage_count,
        ended_at=[None] * stage_count,
    )


def record_terminations(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[int, float]]:
    """Intercept fail-fast termination, returning the list it records into.

    Each entry is the ``(failure_index, cancel_grace)`` the production code
    asked for. Recording rather than signalling keeps these tests free of real
    processes while still proving termination was requested — and requested
    exactly once per fail-fast, which a silent stub could not show.
    """
    terminations: list[tuple[int, float]] = []

    async def fake_terminate(
        processes: object,
        wait_tasks: object,
        failure_index: int,
        *,
        cancel_grace: float,
    ) -> None:
        """Record the termination request instead of signalling processes."""
        del processes, wait_tasks
        await asyncio.sleep(0)
        terminations.append((failure_index, cancel_grace))

    monkeypatch.setattr(
        _pipeline_wait,
        "_terminate_pipeline_remaining_stages",
        fake_terminate,
    )
    return terminations


def pin_clock(monkeypatch: pytest.MonkeyPatch, value: float) -> None:
    """Freeze ``time.perf_counter`` so emitted durations are deterministic."""
    monkeypatch.setattr(_pipeline_wait.time, "perf_counter", lambda: value)
