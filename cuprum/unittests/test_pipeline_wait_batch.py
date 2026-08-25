"""Batch-ordering tests for ``_wait_for_pipeline`` completion processing."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import logging
import typing as typ

from cuprum import _pipeline_wait
from cuprum._pipeline_types import _StageWaitContext
from cuprum.adapters.logging_adapter import structured_logging_hook
from cuprum.unittests._pipeline_wait_support import (
    make_stage_observations,
    pin_clock,
    record_actions,
    record_terminations,
)

if typ.TYPE_CHECKING:
    import pytest

    from cuprum._pipeline_wait import _PipelineWaitResult, _PipelineWaitState
    from cuprum.events import ExecEvent


class _SettledProcess:
    """A process stand-in whose ``wait`` returns immediately."""

    def __init__(self, exit_code: int) -> None:
        """Store the exit code this stand-in reports."""
        self._exit_code = exit_code
        self.returncode: int | None = None

    async def wait(self) -> int:
        """Return the configured exit code without yielding to real I/O."""
        return self._exit_code


@dc.dataclass(frozen=True, slots=True)
class _BatchRun:
    """Everything one all-at-once pipeline wait produced."""

    processed: tuple[int, ...]
    result: _PipelineWaitResult
    terminations: tuple[tuple[int, float], ...]
    events: tuple[ExecEvent, ...]
    records: tuple[logging.LogRecord, ...]


class TestSimultaneousCompletions:
    """Stages completing in one batch resolve by stage order, not set order."""

    @staticmethod
    def _run(
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        exit_codes: list[int],
    ) -> _BatchRun:
        """Settle every stage at once, returning everything the wait published."""
        terminations = record_terminations(monkeypatch)
        pin_clock(monkeypatch, 12.5)

        events: list[ExecEvent] = []
        observations = make_stage_observations(
            len(exit_codes),
            (
                events.append,
                structured_logging_hook(
                    logger=logging.getLogger(_pipeline_wait.__name__)
                ),
            ),
        )

        processed: list[int] = []
        process_completed = _pipeline_wait._process_completed_task

        async def recording(
            task: asyncio.Task[int],
            state: _PipelineWaitState,
            processes: list[asyncio.subprocess.Process],
            cancel_grace: float,
        ) -> None:
            """Note which stage is being processed, then do the real work."""
            processed.append(state.task_to_index[task])
            await process_completed(task, state, processes, cancel_grace)

        monkeypatch.setattr(_pipeline_wait, "_process_completed_task", recording)
        processes = typ.cast(
            "list[asyncio.subprocess.Process]",
            [_SettledProcess(code) for code in exit_codes],
        )

        async def drive() -> _PipelineWaitResult:
            """Await the whole pipeline with every stage already settled."""
            return await _pipeline_wait._wait_for_pipeline(
                processes,
                pipe_tasks=[],
                cancel_grace=0.25,
                stages=_StageWaitContext(
                    started_at=(0.0,) * len(exit_codes),
                    observations=observations,
                ),
            )

        with caplog.at_level(logging.WARNING, logger=_pipeline_wait.__name__):
            result = asyncio.run(drive())

        return _BatchRun(
            processed=tuple(processed),
            result=result,
            terminations=tuple(terminations),
            events=tuple(events),
            records=tuple(caplog.records),
        )

    def test_a_batch_is_processed_in_stage_order(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Every stage in one batch is handled lowest index first."""
        processed = self._run(
            monkeypatch,
            caplog,
            [1, 1, 1, 1, 1, 1, 1, 1],
        ).processed

        assert list(processed) == sorted(processed), (
            f"a batch must be processed in stage order, found {processed!r}"
        )
        assert processed == tuple(range(8)), (
            f"every stage must be processed exactly once, found {processed!r}"
        )

    def test_the_earliest_failing_stage_latches(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The upstream failure is reported, not whichever the set yielded."""
        result = self._run(monkeypatch, caplog, [0, 0, 3, 0, 0, 7, 0]).result

        assert result.failure_index == 2, (
            f"the earliest failing stage must latch, found {result.failure_index!r}"
        )

    def test_a_fully_settled_batch_announces_no_teardown(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failure whose siblings have all already exited terminates nothing."""
        run = self._run(monkeypatch, caplog, [4, 0, 0])

        assert run.result.failure_index == 0, (
            f"the failure must still latch, found {run.result.failure_index!r}"
        )
        actions = record_actions(run.records)
        assert actions == ["pipeline_stage_first_failure"], (
            "a fully settled batch must record the latch but no teardown, "
            f"found {actions!r}"
        )
        assert run.terminations == (), (
            f"no termination may be requested, found {run.terminations!r}"
        )
        assert run.events == (), (
            f"no fail-fast event may be published, found {run.events!r}"
        )
