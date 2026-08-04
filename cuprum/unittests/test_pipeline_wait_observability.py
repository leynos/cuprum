"""Which structured records `_process_completed_task` emits on fail-fast.

All three records reach a default logging configuration at `WARNING`, so they
are user-visible output rather than internal diagnostics, and their fields are
a contract. These tests pin which records each completion sequence produces and
what they carry. The correlation and teardown-outcome fields are covered
separately in `test_pipeline_wait_correlation.py`.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import typing as typ

import pytest

from cuprum import _pipeline_wait
from cuprum.unittests._pipeline_wait_support import (
    CompletionPlan,
    apply_completions,
    drive_completions,
    field_values,
    make_wait_state,
    pin_clock,
    record_actions,
    record_terminations,
    stage_exec_id,
    structured_fields,
)

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

_FIRST_FAILURE_ACTION = "pipeline_stage_first_failure"
_TERMINATION_ACTION = "pipeline_fail_fast_termination"
_TERMINATED_ACTION = "pipeline_fail_fast_terminated"


@dc.dataclass(frozen=True, slots=True)
class _RecordCase:
    """One completion sequence and the records it must produce."""

    stage_count: int
    completions: list[tuple[int, int]]
    expected_actions: list[str]
    reason: str


class TestCompletionObservability:
    """The fail-fast branches emit structured records operators can filter.

    Every record is emitted from `_process_completed_task`, never from the
    pure command or query, so the transition stays free of runtime side
    effects. The clock is monkeypatched, so elapsed times are exact rather
    than dependent on real process timing.
    """

    def test_first_non_final_failure_emits_every_record(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A non-final first failure latches, terminates, and reports both."""
        records = drive_completions(
            monkeypatch,
            caplog,
            CompletionPlan(
                stage_count=3,
                completions=[(0, 4)],
            ),
        )

        assert record_actions(records) == [
            _FIRST_FAILURE_ACTION,
            _TERMINATION_ACTION,
            _TERMINATED_ACTION,
        ], "expected the latch, then termination starting, then its outcome"

        # Every record must carry the same core payload; only the action and
        # the outcome record's own two fields differ.
        core = {
            "cuprum_stage_index": 0,
            "cuprum_stage_count": 3,
            "cuprum_exit_code": 4,
            # Elapsed from the stage's zero start to the injected clock.
            "cuprum_duration_s": 12.5,
            "cuprum_exec_id": stage_exec_id(0),
        }
        outcome_only = {
            "cuprum_action",
            "cuprum_terminated_stage_count",
            "cuprum_termination_duration_s",
        }
        for record in records:
            fields = structured_fields(record)
            assert {
                key: value for key, value in fields.items() if key not in outcome_only
            } == core, (
                "every record carries the stage index, pipeline width, exit "
                "code, elapsed time, and correlation token"
            )

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
                    expected_actions=[
                        _FIRST_FAILURE_ACTION,
                        _TERMINATION_ACTION,
                        _TERMINATED_ACTION,
                    ],
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
        records = drive_completions(
            monkeypatch,
            caplog,
            CompletionPlan(
                stage_count=case.stage_count,
                completions=case.completions,
            ),
        )

        assert record_actions(records) == case.expected_actions, case.reason

    def test_a_completion_timed_before_its_start_reports_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A backwards clock reading clamps to 0.0 rather than going negative.

        Every other case here starts a stage at zero and reads a later clock,
        so they hold whether or not the duration is clamped. This one inverts
        the pair: the stage records a start of 100.0 while the completion reads
        12.5, which without the clamp would publish ``-87.5`` seconds as an
        elapsed time.
        """
        record_terminations(monkeypatch)
        pin_clock(monkeypatch, 12.5)

        state = make_wait_state(3)
        # Recorded after the pinned completion reading, so elapsed is -87.5.
        state.started_at[0] = 100.0

        with caplog.at_level(logging.WARNING, logger=_pipeline_wait.__name__):
            apply_completions(state, [(0, 4)])

        durations = field_values(caplog.records, "cuprum_duration_s")

        assert durations, "a failing stage must emit records carrying a duration"
        assert all(duration == 0.0 for duration in durations), (
            f"a backwards clock must report 0.0, found {durations!r}"
        )

    def test_the_emitted_records_match_their_snapshot(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        snapshot: SnapshotAssertion,
    ) -> None:
        """The whole shape of every record is pinned, not just their fields.

        The assertions above check the pieces each test is about. This pins
        everything a consumer actually sees at once — logger name, level,
        rendered message, and every ``cuprum_`` field — so a change to any of
        them surfaces as a diff rather than passing because no test happened to
        assert on that part.

        These records reach a default logging configuration at ``WARNING``, so
        the level and the message text are as much a contract as the fields.
        The clock is pinned, so the payload is deterministic.
        """
        records = drive_completions(
            monkeypatch,
            caplog,
            CompletionPlan(
                stage_count=3,
                completions=[(0, 4)],
                terminated_count=2,
            ),
        )

        payload = [
            {
                "logger": record.name,
                "level": record.levelname,
                "message": record.getMessage(),
                **structured_fields(record),
            }
            for record in records
        ]

        assert payload == snapshot
