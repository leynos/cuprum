"""What a fail-fast record can be joined to, and what teardown reports.

A stage index and an exit code describe a failure but do not locate it: two
pipelines running concurrently in one process both report a stage 0. These
tests cover the fields that fix that — the per-stage correlation token every
record carries, the pipeline width that makes a record self-describing, and
the outcome reported once fail-fast termination returns.
"""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import pytest

from cuprum import _pipeline_wait
from cuprum._process_lifecycle import (
    _stages_to_terminate,
    _terminate_pipeline_remaining_stages,
)
from cuprum.unittests._pipeline_wait_support import (
    CompletionPlan,
    drive_completions,
    immediate,
    make_wait_state,
    pin_clock,
    record_terminations,
    stage_exec_id,
    structured_fields,
)

_TERMINATION_ACTION = "pipeline_fail_fast_termination"
_TERMINATED_ACTION = "pipeline_fail_fast_terminated"


def _field_values(records: list[logging.LogRecord], key: str) -> list[object]:
    """Return ``key`` from each record that carries it."""
    return [
        fields[key]
        for fields in (structured_fields(record) for record in records)
        if key in fields
    ]


class TestCompletionCorrelation:
    """Records carry the context needed to attribute them to one stage."""

    def test_each_stage_reports_its_own_correlation_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The record carries the failing stage's token, not another stage's.

        The token is what separates concurrent pipelines' records, which only
        holds if it is looked up by the completed stage rather than taken from
        a fixed position such as stage zero or the pipeline's last stage.
        """
        records = drive_completions(
            monkeypatch,
            caplog,
            CompletionPlan(
                stage_count=3,
                completions=[(1, 3)],
            ),
        )

        tokens = set(_field_values(records, "cuprum_exec_id"))

        assert tokens == {stage_exec_id(1)}, (
            f"records must carry stage 1's own token, found {tokens!r}"
        )

    def test_records_report_the_pipeline_width(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The stage count travels with the index, so a record stands alone.

        Without it a reader cannot tell stage 2 of 3 — which has downstream
        stages to stop — from stage 2 of 3 counted differently, nor explain
        why a final-stage failure emits no termination record.
        """
        records = drive_completions(
            monkeypatch,
            caplog,
            CompletionPlan(
                stage_count=4,
                completions=[(1, 2)],
            ),
        )

        counts = set(_field_values(records, "cuprum_stage_count"))

        assert counts == {4}, (
            f"every record must report the four-stage pipeline, found {counts!r}"
        )

    def test_a_state_without_correlation_context_reports_no_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A wait state built without tokens still emits its records.

        The symbolic model and the transition-level tests build states with no
        observation context. Correlation is additive, so its absence must
        degrade to a null token rather than raising out of the fail-fast path.
        """
        record_terminations(monkeypatch)
        pin_clock(monkeypatch, 12.5)

        state = make_wait_state(3)
        state.exec_ids = ()

        async def drive() -> None:
            """Fail stage 0 with no correlation context available."""
            task = asyncio.create_task(immediate(4))
            await task
            state.wait_tasks = [task]
            state.task_to_index = {task: 0}
            await _pipeline_wait._process_completed_task(task, state, [], 0.25)

        with caplog.at_level(logging.WARNING, logger=_pipeline_wait.__name__):
            asyncio.run(drive())

        tokens = _field_values(caplog.records, "cuprum_exec_id")

        assert tokens, "records must still be emitted without correlation context"
        assert all(token is None for token in tokens), (
            f"a missing token must be reported as None, found {tokens!r}"
        )


async def _awaited(future: asyncio.Future[int]) -> int:
    """Await ``future``, giving the helper a real ``Task[int]`` to inspect."""
    return await future


class _StoppableProcess:
    """A process stand-in that settles its wait task when terminated."""

    def __init__(self, wait_future: asyncio.Future[int]) -> None:
        """Hold the future the stand-in resolves once asked to stop."""
        self._wait_future = wait_future

    def terminate(self) -> None:
        """Settle the wait task, standing in for the signal reaching it."""
        if not self._wait_future.done():
            self._wait_future.set_result(-15)

    def kill(self) -> None:
        """Escalate to the same settled result."""
        self.terminate()


class TestTerminationCount:
    """The reported count comes from the helper that does the terminating.

    The record-level tests stub termination out, so they would still pass if
    the helper miscounted. These drive the real helper, which is what makes
    the count in the record trustworthy.
    """

    @staticmethod
    async def _terminate(*, failure_index: int, done: list[bool]) -> int:
        """Run the real helper over stand-in stages, returning its count."""
        loop = asyncio.get_running_loop()
        processes: list[object] = []
        wait_tasks: list[asyncio.Task[int]] = []
        for is_done in done:
            future: asyncio.Future[int] = loop.create_future()
            if is_done:
                future.set_result(0)
            processes.append(_StoppableProcess(future))
            wait_tasks.append(asyncio.create_task(_awaited(future)))
        # Let the already-settled futures mark their tasks done, which is what
        # the helper's selection reads.
        await asyncio.sleep(0)
        return await _terminate_pipeline_remaining_stages(
            typ.cast("list[asyncio.subprocess.Process]", processes),
            wait_tasks,
            failure_index,
            cancel_grace=0.01,
        )

    @pytest.mark.parametrize(
        ("failure_index", "done"),
        [
            pytest.param(0, [True, False, False, False], id="three_still_running"),
            pytest.param(1, [False, True, False], id="failure_in_the_middle"),
            pytest.param(0, [True, True, True], id="all_already_settled"),
        ],
    )
    def test_the_helper_counts_the_stages_it_stopped(
        self,
        failure_index: int,
        done: list[bool],
    ) -> None:
        """The count matches the pure selection of stages needing termination."""
        expected = len(_stages_to_terminate(failure_index, done))

        terminated = asyncio.run(
            self._terminate(failure_index=failure_index, done=done)
        )

        assert terminated == expected, (
            f"expected {expected} stages terminated, the helper reported {terminated}"
        )


class TestTerminationOutcome:
    """Fail-fast teardown reports what it did, not only that it began."""

    def test_the_outcome_reports_the_stages_it_stopped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Termination is reported after it finishes, with what it stopped.

        The starting record is emitted before termination is awaited, so on
        its own it cannot distinguish a teardown that completed from one still
        blocked on a stage that will not die. The outcome record closes that
        gap, and carries the count so a teardown that stopped stages is
        distinguishable from one that found them all already settled.
        """
        records = drive_completions(
            monkeypatch,
            caplog,
            CompletionPlan(
                stage_count=4,
                completions=[(0, 5)],
                terminated_count=2,
            ),
        )

        by_action = {
            str(structured_fields(record)["cuprum_action"]): structured_fields(record)
            for record in records
        }

        assert _TERMINATED_ACTION in by_action, (
            "termination must report an outcome once it returns"
        )
        outcome = by_action[_TERMINATED_ACTION]
        assert outcome["cuprum_terminated_stage_count"] == 2, (
            "the outcome must report how many stages termination stopped"
        )
        # The clock is pinned, so the teardown's own elapsed time is exactly
        # zero; what matters is that the field is present and non-negative.
        assert outcome["cuprum_termination_duration_s"] == 0.0, (
            "a pinned clock must report a zero termination duration"
        )

    def test_the_starting_record_claims_no_outcome(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Only the closing record carries outcome fields.

        The starting record is emitted before termination runs, so reporting a
        count there would be a guess. Keeping the fields on the closing record
        is what lets their presence mean termination actually returned.
        """
        records = drive_completions(
            monkeypatch,
            caplog,
            CompletionPlan(
                stage_count=4,
                completions=[(0, 5)],
                terminated_count=2,
            ),
        )

        starting = next(
            fields
            for fields in (structured_fields(record) for record in records)
            if fields.get("cuprum_action") == _TERMINATION_ACTION
        )

        assert "cuprum_terminated_stage_count" not in starting, (
            "the starting record cannot know the outcome and must not claim one"
        )
        assert "cuprum_termination_duration_s" not in starting, (
            "the starting record must not report a teardown duration"
        )
