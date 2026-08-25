"""Async-boundary tests for `_process_completed_task`.

The command and the query are pure and covered elsewhere. This module covers
the wiring that joins them: reading the clock, applying the command, and
awaiting fail-fast termination when the query says so.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum import _pipeline_wait, _process_lifecycle
from cuprum._pipeline_wait_records import _completion_log_fields
from cuprum.unittests._pipeline_wait_support import (
    advancing_clock,
    apply_completions,
    make_stage_observations,
    make_wait_state,
    pin_clock,
    record_terminations,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

    from cuprum._pipeline_wait import _PipelineWaitState


class _TerminationProcess:
    """Process double that confirms termination by completing its wait future."""

    def __init__(self, waiter: asyncio.Future[int], order: list[str]) -> None:
        """Keep the wait future and shared event order for this process."""
        self._waiter = waiter
        self._order = order
        self.terminate_calls = 0
        self.kill_calls = 0

    def terminate(self) -> None:
        """Record termination and publish the synthetic process exit."""
        self.terminate_calls += 1
        self._order.append("confirmed-termination")
        if not self._waiter.done():
            self._waiter.set_result(-15)

    def kill(self) -> None:
        """Record an unexpected escalation in this immediate-exit double."""
        self.kill_calls += 1


class _CompletionReporter:
    """Capture pipeline-wait records in their publication order."""

    def __init__(self, order: list[str]) -> None:
        """Keep the shared order and captured record extras."""
        self._order = order
        self.records: list[dict[str, object]] = []

    def __call__(self, event: object) -> None:
        """Accept observe events without changing this record-only test."""
        del event

    def report_pipeline_wait(
        self,
        message: str,
        args: tuple[object, ...],
        extra: cabc.Mapping[str, object],
    ) -> None:
        """Record a pipeline-wait action through the adapter port."""
        del message, args
        action = str(extra["cuprum_action"])
        self._order.append(action)
        self.records.append(dict(extra))


@dc.dataclass(slots=True)
class _TerminationScenario:
    """Fixture state for one fail-fast teardown outcome test."""

    state: _PipelineWaitState
    processes: list[asyncio.subprocess.Process]
    late_waiter: asyncio.Future[int]
    confirmed_process: _TerminationProcess
    late_process: _TerminationProcess
    reporter: _CompletionReporter
    order: list[str]


def _make_termination_scenario() -> _TerminationScenario:
    """Create selected, confirmed, and late-settling teardown targets."""
    loop = asyncio.get_running_loop()
    failed_waiter = loop.create_future()
    confirmed_waiter = loop.create_future()
    late_waiter = loop.create_future()
    failed_waiter.set_result(4)
    order: list[str] = []
    confirmed_process = _TerminationProcess(confirmed_waiter, order)
    late_process = _TerminationProcess(late_waiter, order)
    reporter = _CompletionReporter(order)
    state = make_wait_state(
        3,
        observations=make_stage_observations(3, (reporter,)),
    )
    state.wait_tasks = typ.cast(
        "list[asyncio.Task[int]]",
        [failed_waiter, confirmed_waiter, late_waiter],
    )
    return _TerminationScenario(
        state=state,
        processes=typ.cast(
            "list[asyncio.subprocess.Process]",
            [
                _TerminationProcess(failed_waiter, order),
                confirmed_process,
                late_process,
            ],
        ),
        late_waiter=late_waiter,
        confirmed_process=confirmed_process,
        late_process=late_process,
        reporter=reporter,
        order=order,
    )


async def _run_late_settled_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[tuple[bool, ...], _TerminationScenario]:
    """Run the selected-target late-settlement termination race."""
    scenario = _make_termination_scenario()
    fields = _completion_log_fields(scenario.state, 0, 4, 12.5)
    outcomes: list[tuple[bool, ...]] = []
    select_targets = _process_lifecycle._stages_to_terminate
    terminate_stages = _pipeline_wait._terminate_pipeline_remaining_stages

    def settle_after_selection(
        failure_index: int,
        done: list[bool],
    ) -> list[int]:
        """Settle the second target after its selection snapshot."""
        targets = select_targets(failure_index, done)
        scenario.late_waiter.set_result(0)
        return targets

    async def capture_outcomes(
        processes: list[asyncio.subprocess.Process],
        wait_tasks: list[asyncio.Task[int]],
        failure_index: int,
        *,
        cancel_grace: float,
    ) -> tuple[bool, ...]:
        """Run the real teardown and retain each selected target outcome."""
        result = await terminate_stages(
            processes,
            wait_tasks,
            failure_index,
            cancel_grace=cancel_grace,
        )
        outcomes.append(result)
        return result

    monkeypatch.setattr(
        _process_lifecycle,
        "_stages_to_terminate",
        settle_after_selection,
    )
    monkeypatch.setattr(
        _pipeline_wait,
        "_terminate_pipeline_remaining_stages",
        capture_outcomes,
    )
    await _pipeline_wait._terminate_and_report(
        scenario.state,
        scenario.processes,
        0.25,
        fields,
    )
    return outcomes[0], scenario


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
        assert not terminations, "a successful stage must not terminate others"

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
        assert not terminations, "a failing final stage must not request termination"

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


class TestPipelineTerminationOutcomes:
    """Async-boundary tests for verified pipeline termination outcomes."""

    def test_late_settled_target_is_excluded_from_the_reported_count(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only a selected target that completes teardown counts as terminated."""
        outcomes, scenario = asyncio.run(_run_late_settled_termination(monkeypatch))

        assert outcomes == (True, False), (
            "the selected target that settled before teardown must report false"
        )
        assert scenario.confirmed_process.terminate_calls == 1, (
            "the confirmed target must receive one termination request"
        )
        assert scenario.late_process.terminate_calls == 0, (
            "the target that settles before teardown must not be terminated"
        )
        assert scenario.order == [
            "pipeline_fail_fast_termination",
            "confirmed-termination",
            "pipeline_fail_fast_terminated",
        ], "the completion record must follow confirmed termination processing"
        termination_record, outcome_record = scenario.reporter.records
        outcome_fields = {
            "cuprum_terminated_stage_count",
            "cuprum_termination_duration_s",
        }
        assert not outcome_fields & termination_record.keys(), (
            "the termination-start record must not gain outcome-only fields"
        )
        assert outcome_record["cuprum_terminated_stage_count"] == 1, (
            "only the target whose teardown confirms exit may be counted"
        )
        duration = outcome_record["cuprum_termination_duration_s"]
        assert isinstance(duration, float), "termination duration must be a float"
        assert duration >= 0.0, "termination duration must be non-negative"
