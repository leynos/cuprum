"""The structured records `_process_completed_task` emits on fail-fast.

Both records reach a default logging configuration at `WARNING`, so they are
user-visible output rather than internal diagnostics, and their fields are a
contract. These tests pin which records each completion sequence produces and
what they carry.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import logging

import pytest

from cuprum import _pipeline_wait
from cuprum.unittests._pipeline_wait_support import (
    immediate,
    make_wait_state,
    pin_clock,
    record_terminations,
)

_FIRST_FAILURE_ACTION = "pipeline_stage_first_failure"
_TERMINATION_ACTION = "pipeline_fail_fast_termination"


@dc.dataclass(frozen=True, slots=True)
class _RecordCase:
    """One completion sequence and the records it must produce."""

    stage_count: int
    completions: list[tuple[int, int]]
    expected_actions: list[str]
    reason: str


def _structured_fields(record: logging.LogRecord) -> dict[str, object]:
    """Return a record's ``cuprum_``-prefixed structured fields.

    ``extra=`` sets these directly on the record instance, so they are not
    attributes of ``LogRecord`` itself; read them from the instance dictionary
    the way ``cuprum.adapters.logging_adapter`` selects its own fields.
    """
    return {
        key: value for key, value in vars(record).items() if key.startswith("cuprum_")
    }


def _actions(records: list[logging.LogRecord]) -> list[str]:
    """Return the ``cuprum_action`` field of each pipeline-wait record."""
    return [
        str(fields["cuprum_action"])
        for fields in (_structured_fields(record) for record in records)
        if "cuprum_action" in fields
    ]


class TestCompletionObservability:
    """The fail-fast branches emit structured records operators can filter.

    Both records are emitted from `_process_completed_task`, never from the
    pure command or query, so the transition stays free of runtime side
    effects. The clock is monkeypatched, so elapsed times are exact rather
    than dependent on real process timing.
    """

    @staticmethod
    def _run(
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        *,
        stage_count: int,
        completions: list[tuple[int, int]],
    ) -> list[logging.LogRecord]:
        """Drive ``completions`` through the real async boundary, capturing logs.

        Each entry is a ``(stage_index, exit_code)`` pair applied in order.
        ``started_at`` is zero for every stage and the clock is pinned to 12.5,
        so an emitted duration is always exactly 12.5.
        """
        terminations = record_terminations(monkeypatch)
        pin_clock(monkeypatch, 12.5)

        state = make_wait_state(stage_count)

        async def drive() -> None:
            """Apply each completion through ``_process_completed_task``."""
            for idx, exit_code in completions:
                task = asyncio.create_task(immediate(exit_code))
                await task
                state.wait_tasks = [task]
                state.task_to_index = {task: idx}
                await _pipeline_wait._process_completed_task(task, state, [], 0.25)

        with caplog.at_level(logging.WARNING, logger=_pipeline_wait.__name__):
            asyncio.run(drive())

        # Termination bookkeeping must stay consistent with the records.
        expected_terminations = _actions(caplog.records).count(_TERMINATION_ACTION)
        assert len(terminations) == expected_terminations, (
            "each fail-fast termination record must accompany exactly one "
            "termination call"
        )
        return caplog.records

    def test_first_non_final_failure_emits_both_records(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A non-final first failure latches and starts fail-fast termination."""
        records = self._run(
            monkeypatch,
            caplog,
            stage_count=3,
            completions=[(0, 4)],
        )

        assert _actions(records) == [_FIRST_FAILURE_ACTION, _TERMINATION_ACTION], (
            "expected exactly one first-failure record then one termination record"
        )
        # Both records must carry the same payload; only the action differs.
        for record in records:
            fields = _structured_fields(record)
            assert {
                key: value for key, value in fields.items() if key != "cuprum_action"
            } == {
                "cuprum_stage_index": 0,
                "cuprum_exit_code": 4,
                # Elapsed from the stage's zero start to the injected clock.
                "cuprum_duration_s": 12.5,
            }, "both records carry the stage index, exit code, and elapsed time"

    @pytest.mark.parametrize(
        "case",
        [
            pytest.param(
                _RecordCase(
                    stage_count=3,
                    completions=[(1, 0)],
                    expected_actions=[],
                    reason="a successful stage must emit no records",
                ),
                id="successful_completion",
            ),
            pytest.param(
                _RecordCase(
                    stage_count=3,
                    completions=[(2, 1)],
                    expected_actions=[_FIRST_FAILURE_ACTION],
                    reason="a failing final stage must not emit a termination record",
                ),
                id="final_stage_failure",
            ),
            pytest.param(
                _RecordCase(
                    stage_count=1,
                    completions=[(0, 9)],
                    expected_actions=[_FIRST_FAILURE_ACTION],
                    reason="a single-stage pipeline has no other stage to terminate",
                ),
                id="single_stage_failure",
            ),
            pytest.param(
                _RecordCase(
                    stage_count=4,
                    completions=[(0, 1), (1, 1)],
                    expected_actions=[_FIRST_FAILURE_ACTION, _TERMINATION_ACTION],
                    reason="fail-fast reporting must fire exactly once per pipeline",
                ),
                id="later_failure_after_latch",
            ),
        ],
    )
    def test_completion_emits_the_expected_records(
        self,
        case: _RecordCase,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Each completion sequence emits exactly the records it should."""
        records = self._run(
            monkeypatch,
            caplog,
            stage_count=case.stage_count,
            completions=case.completions,
        )

        assert _actions(records) == case.expected_actions, case.reason
