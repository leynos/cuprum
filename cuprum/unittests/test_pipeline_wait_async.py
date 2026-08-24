"""Async-boundary tests for `_process_completed_task`.

The command and the query are pure and covered elsewhere. This module covers
the wiring that joins them: reading the clock, applying the command, and
awaiting fail-fast termination when the query says so.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import logging
import typing as typ

from cuprum import _pipeline_wait
from cuprum._pipeline_types import _StageWaitContext
from cuprum.unittests._pipeline_wait_support import (
    advancing_clock,
    apply_completions,
    make_stage_observations,
    make_wait_state,
    pin_clock,
    record_terminations,
)

if typ.TYPE_CHECKING:
    import pytest

    from cuprum._pipeline_wait import _PipelineWaitResult, _PipelineWaitState
    from cuprum.events import ExecEvent


class TestProcessCompletedTask:
    """Async integration tests for the ``_process_completed_task`` boundary.

    The pure transition is covered elsewhere, as the module docstring says;
    these cover the wiring around it — that the completed task's index and
    result reach ``record_completion``, that the end time comes from the wait
    module's ``perf_counter``, and that termination is invoked exactly when
    ``should_terminate_others`` says so, with the failing stage's index
    forwarded.
    """

    @staticmethod
    def _run_completion(
        monkeypatch: pytest.MonkeyPatch,
        *,
        stage_count: int,
        completed_idx: int,
        exit_code: int,
    ) -> tuple[_PipelineWaitState, list[tuple[int, float]]]:
        """Drive one completed wait task, recording any termination call."""
        terminations = record_terminations(monkeypatch)
        pin_clock(monkeypatch, 12.5)

        state = make_wait_state(stage_count)
        apply_completions(state, [(completed_idx, exit_code)])
        return state, terminations

    def test_records_the_completion_and_terminates_on_first_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A non-final first failure records state and terminates other stages."""
        state, terminations = self._run_completion(
            monkeypatch,
            stage_count=3,
            completed_idx=0,
            exit_code=4,
        )

        assert state.exit_codes[0] == 4, "the task result must reach record_completion"
        assert state.ended_at[0] == 12.5, (
            "the injected perf_counter reading must be stamped as the end time"
        )
        assert state.failure_index == 0, "the failing stage must latch"
        assert terminations == [(0, 0.25)], (
            "termination must run once with the failing index and cancel grace"
        )

    def test_successful_stage_records_without_terminating(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A zero exit is recorded and never triggers termination."""
        state, terminations = self._run_completion(
            monkeypatch,
            stage_count=3,
            completed_idx=1,
            exit_code=0,
        )

        assert state.exit_codes[1] == 0, "the zero exit must be recorded"
        assert state.ended_at[1] == 12.5, "the end time must still be stamped"
        assert state.failure_index is None, "a success must not latch a failure"
        assert terminations == [], "a successful stage must not terminate others"

    def test_failing_final_stage_records_without_terminating(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing final stage latches but has nothing left to terminate."""
        state, terminations = self._run_completion(
            monkeypatch,
            stage_count=3,
            completed_idx=2,
            exit_code=1,
        )

        assert state.failure_index == 2, "the final stage must still latch"
        assert terminations == [], "a failing final stage must not request termination"

    @staticmethod
    def _run_completions(
        monkeypatch: pytest.MonkeyPatch,
        *,
        stage_count: int,
        completions: list[tuple[int, int]],
    ) -> tuple[_PipelineWaitState, list[tuple[int, float]]]:
        """Drive several completions in order, recording termination calls.

        Each entry is a ``(stage_index, exit_code)`` pair applied in sequence,
        so the call order *is* the completion order. The clock advances by ten
        per completion, so each stage's ``ended_at`` is distinguishable rather
        than a single pinned value every slot would match.

        Returns
        -------
        tuple[_PipelineWaitState, list[tuple[int, float]]]
            The driven state and recorded termination requests.
        """
        terminations = record_terminations(monkeypatch)
        clock = advancing_clock(monkeypatch)

        state = make_wait_state(stage_count)

        def advance(step: int) -> None:
            """Move the clock on one tick per completion, not per reading."""
            clock.reading = 10.0 * (step + 1)

        apply_completions(state, completions, before_each=advance)
        return state, terminations

    def test_an_earlier_completion_outranks_a_lower_stage_index(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The first failure *to complete* latches, not the lowest-indexed one.

        Stage 2 fails first, then stage 0 fails. Every other case in this class
        drives a single completion, so none of them can tell completion order
        from stage order — a boundary that latched the lowest index instead
        would pass them all.

        Stage order does decide, but only as a tie-break *within* one
        ``asyncio.wait`` batch, where completion order is unobservable; see
        `TestSimultaneousCompletions`. Across batches, as here, the earlier
        completion wins.
        """
        state, terminations = self._run_completions(
            monkeypatch,
            stage_count=4,
            completions=[(2, 5), (0, 3)],
        )

        assert state.failure_index == 2, (
            "the stage that failed first must stay latched even though a "
            f"lower-indexed stage failed later; found {state.failure_index!r}"
        )
        assert state.exit_codes[2] == 5, "the first completion's exit must be recorded"
        assert state.exit_codes[0] == 3, "the later completion's exit must be recorded"
        assert state.ended_at[2] == 10.0, (
            f"stage 2 must carry the first clock reading, found {state.ended_at[2]!r}"
        )
        assert state.ended_at[0] == 20.0, (
            f"stage 0 must carry the second clock reading, found {state.ended_at[0]!r}"
        )
        assert terminations == [(2, 0.25)], (
            "only the latched first failure may terminate the others, exactly "
            f"once; found {terminations!r}"
        )


class _SettledProcess:
    """A process stand-in whose ``wait`` returns immediately.

    ``_wait_for_pipeline`` only ever calls ``wait`` on the processes it is
    given, so this is enough to put every stage into a single
    ``asyncio.wait`` batch — the case where completion order runs out.
    """

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
    """Stages completing in one batch resolve by stage order, not set order.

    ``asyncio.wait`` returns an unordered set, so when several stages settle
    together there is no completion order left to observe. Reporting whichever
    the set happened to yield first would make ``failure_index`` — and the
    structured record naming the failing stage — vary between runs on
    identical input.
    """

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
        observations = make_stage_observations(len(exit_codes), (events.append,))

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

        # `_wait_for_pipeline` only ever calls `wait` on these, so the
        # stand-ins satisfy the part of the protocol that is actually used.
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

        # Snapshot the mutable collectors here: the run is over, so the tuples
        # cannot drift from what it published.
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
        processed = self._run(monkeypatch, caplog, [1, 1, 1, 1, 1, 1, 1, 1]).processed

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
        """The upstream failure is reported, not whichever the set yielded.

        Stages 2 and 5 both fail in the same batch. Stage 2 is upstream, so it
        is the one that caused stage 5's failure and the one an operator needs
        named.
        """
        result = self._run(monkeypatch, caplog, [0, 0, 3, 0, 0, 7, 0]).result

        assert result.failure_index == 2, (
            f"the earliest failing stage must latch, found {result.failure_index!r}"
        )

    def test_a_fully_settled_batch_announces_no_teardown(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failure whose siblings have all already exited terminates nothing.

        Every stage settles into one ``asyncio.wait`` batch, and the batch is
        processed in stage order, so by the time stage 0's failure is handled
        stages 1 and 2 have exited on their own. ``should_terminate_others``
        still answers ``True`` — it reasons about stage positions, not about
        which processes are alive — so without the runtime check the pipeline
        would log a teardown it never performed, publish a
        ``pipeline_fail_fast`` event, and increment
        ``cuprum_pipeline_fail_fast_total`` for a decision with no subject. The
        closing record would then report
        ``cuprum_terminated_stage_count`` of zero, contradicting the record
        that opened it.

        The latch itself is unaffected: the failure happened, ``failure_index``
        reports it, and the first-failure record is what tells an operator
        which stage it was.
        """
        run = self._run(monkeypatch, caplog, [4, 0, 0])

        assert run.result.failure_index == 0, (
            f"the failure must still latch, found {run.result.failure_index!r}"
        )
        actions = [str(vars(record)["cuprum_action"]) for record in run.records]
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
