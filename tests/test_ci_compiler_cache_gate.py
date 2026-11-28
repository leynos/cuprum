"""Contracts for the adoption-gate arithmetic over paired observations.

The reader validates records elsewhere; these tests choose observations whose
whole-job timings and job results make one gate term decisive, then assert the
verdict. Each case has a plausible wrong implementation that would adopt a
family the evidence does not support, and each asserts against that.
"""

from __future__ import annotations

import doctest

import pytest

from scripts import compiler_cache_summary as summary_reader
from tests.helpers.ci_compiler_gate import (
    Observation,
    console,
    decisions,
    family,
    five_pairs,
    jobs,
    observations,
    summary,
)


def test_the_summary_module_examples_run() -> None:
    """The `_worth_it` examples are executable contracts, not prose."""
    result = doctest.testmod(summary_reader, verbose=False)
    assert result.failed == 0, f"{result.failed} module examples failed"


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


def test_an_unknown_threshold_is_insufficient_not_a_rejection() -> None:
    """A missing transfer measurement is not evidence of a small saving."""
    records, job_documents = five_pairs(save=None)
    built = summary(records, job_documents, console())
    decision = decisions(built)[0]
    assert decision["minimum_worthwhile_seconds"] is None, decision
    assert decision["verdict"] == "insufficient-evidence", (
        f"a missing threshold must not become reject, got {decision}"
    )


def test_break_even_stays_unknown_until_billing_is_verified() -> None:
    """An unverified rounding rule cannot support a consumer count."""
    records, job_documents = five_pairs()
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
    records, job_documents = five_pairs()
    matched = summary(
        records,
        job_documents,
        console(
            incremental_cost_seconds=0,
            test_fidelity="matched",
            billing_rounding="verified",
        ),
    )
    assert decisions(matched)[0]["verdict"] == "adopt", (
        f"the same evidence with matched fidelity must adopt, got {decisions(matched)}"
    )
    unknown = summary(
        records,
        job_documents,
        console(incremental_cost_seconds=0, billing_rounding="verified"),
    )
    assert decisions(unknown)[0]["verdict"] == "insufficient-evidence", (
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


@pytest.mark.parametrize(
    ("control", "candidate"),
    [
        (
            Observation(run_id="100", backend="local", hits=4, toolchain="1.87.0"),
            Observation(
                run_id="200",
                backend="gha",
                hits=6,
                restore=None,
                save=None,
                toolchain="nightly-2026-08-23",
            ),
        ),
        (
            Observation(run_id="100", backend="local", hits=4),
            Observation(
                run_id="200",
                backend="gha",
                hits=6,
                restore=None,
                save=None,
                runner_class="github-hosted",
            ),
        ),
        (
            Observation(run_id="100", backend="local", hits=4, rustc_release="1.87.0"),
            Observation(
                run_id="200",
                backend="gha",
                hits=6,
                restore=None,
                save=None,
                rustc_release="1.87.1",
            ),
        ),
        (
            Observation(run_id="100", backend="local", hits=4, compiler_shape="debug"),
            Observation(
                run_id="200",
                backend="gha",
                hits=6,
                restore=None,
                save=None,
                compiler_shape="release",
            ),
        ),
        (
            Observation(run_id="100", backend="local", hits=4, python_version="3.13"),
            Observation(
                run_id="200",
                backend="gha",
                hits=6,
                restore=None,
                save=None,
                python_version="3.14",
            ),
        ),
    ],
    ids=["toolchain", "runner-class", "rustc", "compiler-shape", "python-version"],
)
def test_an_incomparable_pair_is_excluded(
    control: Observation, candidate: Observation
) -> None:
    """A provenance or runner difference must not be read as a cache saving."""
    built = summary(
        [control, candidate], jobs([100, 200], 240), console(test_fidelity="matched")
    )
    observed = family(built)
    assert observed["pairs"] == 0, (
        f"an incomparable pair must not count, got {observed}"
    )
    assert observed["incomparable_pairs"] == 1, (
        f"the excluded pair must be visible, got {observed}"
    )


@pytest.mark.parametrize(
    ("control", "candidate"),
    [
        (
            Observation(run_id="100", backend="local", hits=4, runner_class="unknown"),
            Observation(
                run_id="200",
                backend="gha",
                hits=6,
                restore=None,
                save=None,
                runner_class="unknown",
            ),
        ),
        (
            Observation(run_id="100", backend="local", hits=4, rustc_release="unknown"),
            Observation(
                run_id="200",
                backend="gha",
                hits=6,
                restore=None,
                save=None,
                rustc_release="unknown",
            ),
        ),
    ],
    ids=["scalar", "compiler-identity"],
)
def test_unknown_provenance_never_pairs(
    control: Observation, candidate: Observation
) -> None:
    """A scalar or an all-unknown compiler identity excludes the pair."""
    built = summary([control, candidate], jobs([100, 200], 240))
    observed = family(built)
    assert observed["pairs"] == 0, (
        f"an unknown provenance value must not pair, got {observed}"
    )
    assert observed["incomparable_pairs"] == 1, (
        f"the excluded pair must be visible, got {observed}"
    )


def test_a_noncompliant_candidate_never_pairs() -> None:
    """A store that differs from the selection is not success evidence."""
    records = [
        Observation(run_id="100", backend="local", hits=4),
        Observation(
            run_id="200",
            backend="gha",
            hits=6,
            restore=None,
            save=None,
            state="noncompliant",
        ),
    ]
    built = summary(records, jobs([100, 200], 240))
    assert family(built)["pairs"] == 0, (
        f"a noncompliant candidate must not pair, got {family(built)}"
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


def test_unverified_billing_blocks_adoption() -> None:
    """A paid cost is unestablished while its rounding rule is unknown."""
    records, job_documents = five_pairs()
    built = summary(
        records,
        job_documents,
        console(
            incremental_cost_seconds=0,
            test_fidelity="matched",
            billing_rounding="unknown",
        ),
    )
    assert decisions(built)[0]["verdict"] == "insufficient-evidence", (
        "an unverified cost must not reach adopt"
    )


def test_an_unknown_backend_never_pairs() -> None:
    """Only an identified local control or gha candidate may pair."""
    records = [
        Observation(run_id="100", backend="unknown", hits=4),
        Observation(run_id="200", backend="gha", hits=6, restore=None, save=None),
    ]
    built = summary(records, jobs([100, 200], 240))
    assert family(built)["pairs"] == 0, (
        f"an unidentified backend must not become a control, got {built['families']}"
    )


def test_a_degraded_record_is_labelled_a_fallback() -> None:
    """Any non-ok state is the fallback-or-misconfiguration class."""
    built = summary(
        [Observation(run_id="100", backend="local", hits=4, state="degraded")],
        jobs([100], 240),
    )
    labelled = observations(built)
    assert labelled[0]["observation_class"] == "fallback-or-misconfiguration", (
        f"a degraded record must be a fallback, got {labelled}"
    )
