"""Property contracts for the gate arithmetic, classification, and pairing.

The example tests pin the named boundaries. These properties cover the wider
input space: a threshold comparison over any finite triple, classification
that does not depend on input order, pairing bounded by the shorter arm, and
duplicated records that cannot inflate a pair count or a paid total.
"""

from __future__ import annotations

import typing as typ

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from scripts import compiler_cache_cost as cost_reader
from scripts import compiler_cache_pairs as pairs_reader
from scripts import compiler_cache_schema as schema
from scripts.compiler_cache_summary import _minimum_worthwhile, _worth_it
from tests.helpers.ci_compiler_evidence import record_fixture
from tests.helpers.ci_compiler_gate import Observation, console, family, jobs, summary

if typ.TYPE_CHECKING:
    import collections.abc as cabc

HYPOTHESIS = settings(
    deadline=None,
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@HYPOTHESIS
@given(
    saving=st.floats(allow_nan=False, allow_infinity=False),
    floor=st.floats(allow_nan=False, allow_infinity=False),
    transfer=st.floats(allow_nan=False, allow_infinity=False),
)
def test_worth_it_matches_its_documented_thresholds(
    saving: float, floor: float, transfer: float
) -> None:
    """The gate accepts exactly the documented inclusive floor and strict transfer."""
    assert _worth_it(saving, floor, transfer) is (
        saving >= floor and saving > transfer
    ), f"{saving=}, {floor=}, {transfer=} must follow the documented comparison"
    assert _minimum_worthwhile(floor, transfer) == max(floor, transfer), (
        "the minimum worthwhile saving is the larger threshold"
    )


def _warm_records(count: int) -> list[dict[str, object]]:
    """Return warm local observations of distinct pull-request runs."""
    counters = dict.fromkeys(schema.COUNTERS, None)
    counters["compile_requests"] = 5
    counters["cache_hits"] = 1
    return [
        record_fixture(
            bracket=f"bracket-{index}",
            run_id=str(100 + index),
            recorded_at=f"2026-11-22T00:00:{index:02d}Z",
            pr_number="42",
            counters=counters,
        )
        for index in range(count)
    ]


@HYPOTHESIS
@given(order=st.permutations(range(5)))
def test_classification_is_independent_of_input_order(
    order: cabc.Sequence[int],
) -> None:
    """Shuffling the records must not change any record's label."""
    records = _warm_records(5)
    shuffled = [records[index] for index in order]
    labels = {
        str(entry["bracket"]): entry["observation_class"]
        for entry in pairs_reader.classify_observations(shuffled, [], {})
    }
    expected = {
        str(record["bracket"]): (
            "first-pr-consumer" if index == 0 else "subsequent-pr-push"
        )
        for index, record in enumerate(records)
    }
    assert labels == expected, (
        f"the labels must depend on recorded_at alone, got {labels}"
    )


@HYPOTHESIS
@given(
    controls=st.integers(min_value=1, max_value=6),
    candidates=st.integers(min_value=1, max_value=6),
)
def test_pair_count_is_bounded_by_the_shorter_arm(
    controls: int, candidates: int
) -> None:
    """Positional pairing stops at the shorter arm, as the gate documents."""
    records = [
        Observation(run_id=str(100 + index), backend="local", hits=4)
        for index in range(controls)
    ]
    records += [
        Observation(
            run_id=str(200 + index), backend="gha", hits=6, restore=None, save=None
        )
        for index in range(candidates)
    ]
    job_documents = jobs([*range(100, 100 + controls)], 240)
    job_documents += jobs([*range(200, 200 + candidates)], 195)
    built = summary(records, job_documents, console())
    observed = family(built)
    assert observed["independent_job_pairs"] == min(controls, candidates), observed
    assert observed["pairs"] == min(controls, candidates), observed


@HYPOTHESIS
@given(duplicates=st.integers(min_value=1, max_value=3))
def test_duplicate_records_do_not_inflate_pairs(duplicates: int) -> None:
    """Repeated brackets of one job are one independent pair, not several."""
    controls = [
        Observation(run_id=str(100 + index), backend="local", hits=4)
        for index in range(3)
    ]
    candidate = Observation(
        run_id="200", backend="gha", hits=6, restore=None, save=None
    )
    records = controls + [candidate for _ in range(1 + duplicates)]
    job_documents = jobs([*range(100, 103)], 240) + jobs([200], 195)
    built = summary(records, job_documents, console())
    observed = family(built)
    assert observed["independent_job_pairs"] == 1, observed
    assert observed["pairs"] == 1, observed


@HYPOTHESIS
@given(duplicates=st.integers(min_value=1, max_value=4))
def test_duplicate_brackets_do_not_change_the_paid_total(duplicates: int) -> None:
    """One job's paid seconds are charged once however many brackets it has."""
    counters = dict.fromkeys(schema.COUNTERS, None)
    counters["compile_requests"] = 5
    base = record_fixture(bracket="typecheck", counters=counters)
    twins = [
        record_fixture(bracket=f"twin-{index}", counters=counters)
        for index in range(duplicates)
    ]
    job = {
        "run_id": 1,
        "run_attempt": 1,
        "name": "typecheck-test",
        "started_at": "2026-11-22T00:00:00Z",
        "completed_at": "2026-11-22T00:01:40Z",
        "conclusion": "success",
    }
    names = {"typecheck-test": "typecheck-test"}
    once = cost_reader.cost([base], [job], names, None)
    many = cost_reader.cost([base, *twins], [job], names, None)
    assert once["paid_job_seconds_by_workflow"] == {"ci.yml": 100.0}, once
    assert (
        many["paid_job_seconds_by_workflow"] == once["paid_job_seconds_by_workflow"]
    ), many
