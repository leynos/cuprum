"""Contracts for the adoption-gate arithmetic over paired observations.

The reader validates records elsewhere; these tests choose observations whose
whole-job timings and job results make one gate term decisive, then assert the
verdict. Each case has a plausible wrong implementation that would adopt a
family the evidence does not support, and each asserts against that.
"""

from __future__ import annotations

import typing as typ

import pytest

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


def test_failed_jobs_are_counted_but_never_pair_savings() -> None:
    """A failing candidate must not satisfy the gate through a shorter job."""
    records = [
        Observation(run_id=str(100 + index), backend="local", hits=4)
        for index in range(5)
    ]
    records += [
        Observation(run_id=str(200 + index), backend="gha", hits=6)
        for index in range(5)
    ]
    job_documents = jobs([*range(100, 105)], 240)
    job_documents += jobs([*range(200, 205)], 180, conclusion="failure")
    built = summary(records, job_documents)
    observed = family(built)
    assert observed["failures"] == 5, (
        f"each failed candidate must be counted, got {observed}"
    )
    assert observed["pairs"] == 0, (
        f"a failed candidate must contribute no saving pair, got {observed}"
    )
    assert decisions(built)[0]["verdict"] == "insufficient-evidence", (
        "a family with no passing pair cannot be adopted"
    )


def test_two_brackets_in_one_job_are_one_independent_pair() -> None:
    """Bracket pairs would multiply the sample and satisfy the gate early."""
    records: list[Observation] = [
        Observation(run_id="100", backend="local", hits=4, bracket="typecheck"),
        Observation(run_id="100", backend="local", hits=4, bracket="python-tests"),
        Observation(
            run_id="200",
            backend="gha",
            hits=6,
            bracket="typecheck",
            restore=None,
            save=None,
        ),
        Observation(
            run_id="200",
            backend="gha",
            hits=6,
            bracket="python-tests",
            restore=None,
            save=None,
        ),
    ]
    job_documents: list[dict[str, object]] = jobs([100, 200], 240)
    built = summary(records, job_documents)
    observed = family(built)
    assert observed["pairs"] == 1, (
        f"two brackets of one job are one independent pair, got {observed}"
    )
    assert observed["independent_job_pairs"] == 1, (
        f"the independent pair must be recorded, got {observed}"
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


def test_a_negative_incremental_cost_is_accepted() -> None:
    """The gate requires non-positive cost, not exactly zero."""
    records = [
        Observation(run_id=str(100 + index), backend="local", hits=4)
        for index in range(5)
    ]
    records += [
        Observation(
            run_id=str(200 + index), backend="gha", hits=6, restore=None, save=None
        )
        for index in range(5)
    ]
    job_documents = jobs([*range(100, 105)], 240)
    job_documents += jobs([*range(200, 205)], 195)
    built = summary(
        records,
        job_documents,
        console(incremental_cost_seconds=-10, test_fidelity="matched"),
    )
    decision = decisions(built)[0]
    assert decision["incremental_cost_seconds"] == pytest.approx(-10.0), decision
    assert decision["verdict"] == "adopt", decision


def _five_pairs(
    *, save: float | None = 20.0, candidate_seconds: int = 195
) -> tuple[list[Observation], list[dict[str, object]]]:
    """Return five control/candidate pairs and their API jobs."""
    records = [
        Observation(run_id=str(100 + index), backend="local", hits=4, save=save)
        for index in range(5)
    ]
    records += [
        Observation(
            run_id=str(200 + index), backend="gha", hits=6, restore=None, save=None
        )
        for index in range(5)
    ]
    job_documents = jobs([*range(100, 105)], 240)
    job_documents += jobs([*range(200, 205)], candidate_seconds)
    return records, job_documents


def test_an_unknown_threshold_is_insufficient_not_a_rejection() -> None:
    """A missing transfer measurement is not evidence of a small saving."""
    records, job_documents = _five_pairs(save=None)
    built = summary(records, job_documents, console())
    decision = decisions(built)[0]
    assert decision["minimum_worthwhile_seconds"] is None, decision
    assert decision["verdict"] == "insufficient-evidence", (
        f"a missing threshold must not become reject, got {decision}"
    )


def test_break_even_stays_unknown_until_billing_is_verified() -> None:
    """An unverified rounding rule cannot support a consumer count."""
    records, job_documents = _five_pairs()
    unknown = summary(records, job_documents, console(billing_rounding="unknown"))
    assert decisions(unknown)[0]["break_even_consumers"] is None, (
        "break-even must stay unknown while billing is unverified"
    )
    verified = summary(records, job_documents, console(billing_rounding="verified"))
    assert decisions(verified)[0]["break_even_consumers"] == 3, (
        f"60 s population plus 40 s transfer over 45 s is 3 consumers, got "
        f"{decisions(verified)[0]}"
    )


def test_unknown_test_fidelity_blocks_adoption() -> None:
    """The gate cannot adopt a candidate whose tests were not compared."""
    records = [
        Observation(run_id=str(100 + index), backend="local", hits=4)
        for index in range(5)
    ]
    records += [
        Observation(
            run_id=str(200 + index), backend="gha", hits=6, restore=None, save=None
        )
        for index in range(5)
    ]
    job_documents = jobs([*range(100, 105)], 240)
    job_documents += jobs([*range(200, 205)], 195)
    built = summary(records, job_documents, console(incremental_cost_seconds=0))
    assert decisions(built)[0]["verdict"] == "insufficient-evidence", (
        "unknown test fidelity must not reach adopt"
    )


def test_a_family_with_only_cold_records_still_reports_a_decision() -> None:
    """A cold family is evidence, not an absence of observations."""
    records = [
        Observation(run_id="100", backend="local", hits=0, restore_hit=False),
    ]
    built = summary(records, jobs([100], 240))
    assert family(built)["pairs"] == 0, "a cold family cannot pair"
    assert decisions(built)[0]["verdict"] == "insufficient-evidence", (
        f"a cold family must still be reported, got {built['gate']}"
    )


def test_pairs_with_different_compiler_provenance_are_excluded() -> None:
    """A toolchain difference measures the toolchain, not the cache backend."""
    records = [
        Observation(run_id="100", backend="local", hits=4, toolchain="1.87.0"),
        Observation(
            run_id="200",
            backend="gha",
            hits=6,
            restore=None,
            save=None,
            toolchain="nightly-2026-08-23",
        ),
    ]
    built = summary(records, jobs([100, 200], 240), console(test_fidelity="matched"))
    observed = family(built)
    assert observed["pairs"] == 0, (
        f"an incomparable pair must not count, got {observed}"
    )
    assert observed["incomparable_pairs"] == 1, (
        f"the excluded pair must be visible, got {observed}"
    )


def test_a_failed_fallback_is_counted_even_though_it_cannot_pair() -> None:
    """A compiler-only fallback must not hide a failing job."""
    records = [
        Observation(run_id=str(100 + index), backend="local", hits=4)
        for index in range(5)
    ]
    records += [
        Observation(
            run_id=str(200 + index),
            backend="gha",
            hits=6,
            restore=None,
            save=None,
            state="degraded",
        )
        for index in range(5)
    ]
    job_documents = jobs([*range(100, 105)], 240)
    job_documents += jobs([*range(200, 205)], 180, conclusion="failure")
    built = summary(records, job_documents, console())
    observed = family(built)
    assert observed["pairs"] == 0, "a degraded candidate must not pair"
    assert observed["failures"] == 5, (
        f"the failed fallbacks must still be counted, got {observed}"
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
