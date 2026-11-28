"""Contracts for the terms a family must satisfy before adoption.

The gate tests cover the console-derived thresholds; these tests cover the
saving, cost, protection, and sample-size terms. Each case makes exactly one
term decisive, so removing that term changes the verdict.
"""

from __future__ import annotations

import pytest

from tests.helpers.ci_compiler_gate import (
    Observation,
    console,
    decisions,
    family,
    five_pairs,
    jobs,
    summary,
)


def test_a_negative_incremental_cost_is_accepted() -> None:
    """The gate requires non-positive cost, not exactly zero."""
    records, job_documents = five_pairs()
    built = summary(
        records,
        job_documents,
        console(
            incremental_cost_seconds=-10,
            test_fidelity="matched",
            billing_rounding="verified",
        ),
    )
    decision = decisions(built)[0]
    assert decision["incremental_cost_seconds"] == pytest.approx(-10.0), decision
    assert decision["verdict"] == "adopt", decision


def test_a_saving_below_the_five_percent_floor_is_rejected() -> None:
    """A known saving below the declared floor is a rejection, not a wait."""
    records, job_documents = five_pairs(candidate_seconds=235)
    built = summary(records, job_documents, console())
    assert decisions(built)[0]["verdict"] == "reject", (
        f"a 5 s saving against a 12 s floor must reject, got {decisions(built)}"
    )


def test_a_saving_equal_to_the_transfer_is_not_adopted() -> None:
    """The saving must be strictly larger than the transfer it replaces."""
    records, job_documents = five_pairs(candidate_seconds=200)
    built = summary(
        records,
        job_documents,
        console(
            incremental_cost_seconds=0,
            test_fidelity="matched",
            billing_rounding="verified",
        ),
    )
    decision = decisions(built)[0]
    assert decision["save_restore_seconds"] == pytest.approx(40.0), decision
    assert decision["median_saving_seconds"] == pytest.approx(40.0), decision
    assert decision["verdict"] != "adopt", decision


def test_a_five_percent_floor_equal_to_the_saving_is_adoptable() -> None:
    """The floor comparison is inclusive; only the transfer is strict."""
    records = [
        Observation(
            run_id=str(100 + index), backend="local", hits=4, restore=0.0, save=1.0
        )
        for index in range(5)
    ]
    records += [
        Observation(
            run_id=str(200 + index),
            backend="gha",
            hits=6,
            restore=None,
            save=None,
        )
        for index in range(5)
    ]
    job_documents = jobs([*range(100, 105)], 100)
    job_documents += jobs([*range(200, 205)], 95)
    built = summary(
        records,
        job_documents,
        console(
            incremental_cost_seconds=0,
            test_fidelity="matched",
            billing_rounding="verified",
        ),
    )
    decision = decisions(built)[0]
    assert decision["five_percent_floor_seconds"] == pytest.approx(5.0), decision
    assert decision["median_saving_seconds"] == pytest.approx(5.0), decision
    assert decision["verdict"] == "adopt", decision


def test_four_pairs_are_insufficient() -> None:
    """The gate requires five independent pairs, so four cannot adopt."""
    records = [
        Observation(run_id=str(100 + index), backend="local", hits=4)
        for index in range(4)
    ]
    records += [
        Observation(
            run_id=str(200 + index), backend="gha", hits=6, restore=None, save=None
        )
        for index in range(4)
    ]
    job_documents = jobs([*range(100, 104)], 240)
    job_documents += jobs([*range(200, 204)], 195)
    built = summary(
        records,
        job_documents,
        console(
            incremental_cost_seconds=0,
            test_fidelity="matched",
            billing_rounding="verified",
        ),
    )
    observed = family(built)
    assert observed["pairs"] == 4, observed
    assert decisions(built)[0]["verdict"] == "insufficient-evidence", decisions(built)


@pytest.mark.parametrize(
    ("console_fields", "verdict"),
    [
        (
            {
                "incremental_cost_seconds": 0,
                "test_fidelity": "matched",
                "billing_rounding": "verified",
            },
            "adopt",
        ),
        (
            {
                "branch_protection": "unknown",
                "incremental_cost_seconds": 0,
                "test_fidelity": "matched",
                "billing_rounding": "verified",
            },
            "insufficient-evidence",
        ),
        (
            {
                "incremental_cost_seconds": 1,
                "test_fidelity": "matched",
                "billing_rounding": "verified",
            },
            "insufficient-evidence",
        ),
    ],
    ids=["baseline-adopt", "protection-off", "positive-cost"],
)
def test_every_adoption_term_is_required(
    console_fields: dict[str, object], verdict: str
) -> None:
    """A saving above the transfer adopts only when every other term holds."""
    records, job_documents = five_pairs()
    built = summary(records, job_documents, console(**console_fields))
    assert decisions(built)[0]["verdict"] == verdict, decisions(built)
