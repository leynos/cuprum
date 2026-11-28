"""Contracts for whole-job cost, transfer time, and the critical path.

The gate tests choose observations that decide an adoption term; these tests
choose jobs and durations that decide a cost figure. A bracket pair must not
treble a job's paid seconds, a missing timestamp must stay unknown, and a
finite operand pair must not overflow into a non-finite total.
"""

from __future__ import annotations

import typing as typ

import pytest

from scripts import compiler_cache_cost as cost_reader
from tests.helpers.ci_compiler_gate import (
    Observation,
    console,
    decisions,
    family,
    jobs,
    summary,
)


def test_the_transfer_is_the_sum_of_restore_and_save_per_job() -> None:
    """A median of individual timings would halve the cost a migration removes."""
    records = [
        Observation(run_id="100", backend="local", hits=4),
        Observation(run_id="200", backend="gha", hits=6, restore=None, save=None),
    ]
    built = summary(records, jobs([100, 200], 240))
    observed = family(built)
    assert observed["median_transfer_seconds"] == pytest.approx(40.0), (
        f"20 s restore plus 20 s save must report 40 s, got {observed}"
    )


def test_a_control_without_a_save_time_keeps_the_transfer_unknown() -> None:
    """A pull-request control never saves; inventing zero would understate cost."""
    records = [
        Observation(run_id="100", backend="local", hits=4, save=None),
        Observation(run_id="200", backend="gha", hits=6, restore=None, save=None),
    ]
    built = summary(records, jobs([100, 200], 240))
    assert family(built)["median_transfer_seconds"] is None, (
        "a missing save time must keep the total unknown"
    )


def test_job_cost_is_charged_once_for_several_brackets() -> None:
    """Three brackets in a 100-second job must not report 300 job-seconds."""
    records = [
        Observation(run_id="100", backend="local", hits=4, bracket=name)
        for name in ("typecheck", "python-tests", "extension")
    ]
    built = summary(records, jobs([100], 100))
    cost = typ.cast("dict[str, object]", built["cost"])
    assert cost["paid_job_seconds_by_workflow"] == {"ci.yml": 100.0}, (
        f"one job with three brackets must be charged once, got {cost}"
    )


def test_a_non_finite_transfer_total_stays_unknown() -> None:
    """Two finite durations can sum to infinity; the report must stay finite."""
    records = [
        Observation(run_id="100", backend="local", hits=4, restore=1e308, save=1e308),
        Observation(run_id="200", backend="gha", hits=6, restore=None, save=None),
    ]
    built = summary(records, jobs([100, 200], 240), console())
    assert family(built)["median_transfer_seconds"] is None, (
        f"a non-finite transfer must be unknown, got {family(built)}"
    )


def test_a_non_finite_break_even_stays_unknown() -> None:
    """A finite population plus a finite transfer can still overflow the ratio."""
    records = [
        Observation(run_id="100", backend="local", hits=4, restore=8e307, save=8e307),
        Observation(run_id="200", backend="gha", hits=6, restore=None, save=None),
    ]
    job_documents = jobs([100], 240) + jobs([200], 195)
    built = summary(
        records,
        job_documents,
        console(initial_population_seconds=1e308, billing_rounding="verified"),
    )
    decision = decisions(built)[0]
    assert decision["save_restore_seconds"] == pytest.approx(1.6e308), decision
    assert decision["break_even_consumers"] is None, (
        f"an overflowing ratio must be unknown, got {decision}"
    )


def test_the_critical_path_includes_jobs_without_evidence() -> None:
    """A long prerequisite is part of the run's critical path."""
    records = [Observation(run_id="100", backend="local", hits=4)]
    job_documents: list[dict[str, object]] = [
        {
            "run_id": 100,
            "run_attempt": 1,
            "name": "Prerequisite",
            "started_at": "2026-11-22T00:00:00Z",
            "completed_at": "2026-11-22T00:10:00Z",
            "conclusion": "success",
        },
        {
            "run_id": 100,
            "run_attempt": 1,
            "name": "Extension-gated tests",
            "started_at": "2026-11-22T00:10:00Z",
            "completed_at": "2026-11-22T00:20:00Z",
            "conclusion": "success",
        },
    ]
    built = summary(records, job_documents)
    cost = typ.cast("dict[str, object]", built["cost"])
    assert cost["critical_path_seconds_by_workflow"] == {"ci.yml": 1200.0}, (
        f"the unrecorded prerequisite must be included, got {cost}"
    )


def test_a_run_without_timestamps_reports_an_unknown_critical_path() -> None:
    """A missing jobs document leaves the span unknown, not a crash."""
    records = [Observation(run_id="100", backend="local", hits=4)]
    built = summary(records, [])
    cost = typ.cast("dict[str, object]", built["cost"])
    assert cost["critical_path_seconds_by_workflow"] is None, (
        f"a run without timestamps must stay unknown, got {cost}"
    )


def test_a_recorded_job_without_a_duration_makes_the_total_unknown() -> None:
    """A partial paid total must not read as the complete cost."""
    records = [
        Observation(run_id="100", backend="local", hits=4),
        Observation(run_id="200", backend="local", hits=4, bracket="python-tests"),
    ]
    job_documents = jobs([100], 100)
    built = summary(records, job_documents)
    cost = typ.cast("dict[str, object]", built["cost"])
    assert cost["paid_job_seconds_by_workflow"] == {"ci.yml": None}, (
        f"an unpriceable job must make that workflow's total unknown, got {cost}"
    )


def test_console_number_accepts_a_negative_only_when_signed() -> None:
    """Durations cannot be negative; an incremental saving can."""
    console = {"incremental_cost_seconds": -5}
    assert cost_reader.console_number(
        console, "incremental_cost_seconds", signed=True
    ) == pytest.approx(-5.0), "a signed saving must be returned unchanged"
    assert cost_reader.console_number(console, "incremental_cost_seconds") is None, (
        "an unsigned duration must reject a negative value"
    )
