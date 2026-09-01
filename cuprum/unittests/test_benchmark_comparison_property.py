"""Property-based tests for the benchmark comparison reducers.

``benchmarks.comparison_analysis`` matches Python and Rust benchmark scenarios
and reduces them into a report. The example-based tests cover a handful of
curated payloads; these properties pin the reducer invariants across generated
inputs:

- ``_build_row``: ``speedup_ratio == python_mean / rust_mean`` and the
  faster-backend trichotomy (tie within tolerance, else the smaller mean wins).
- ``_build_report_from_grouped_entries``: rows are sorted by ``comparison_id``,
  ``row_count`` equals the number of groups, and the win/tie tally partitions
  the rows exactly.
- ``compare_candidate_backend_results``: duplicate backend entries and
  plan/result count mismatches are rejected.
"""

from __future__ import annotations

import math
from collections import Counter

import pytest
from hypothesis import given
from hypothesis import strategies as st

from benchmarks.comparison_analysis import (
    _build_report_from_grouped_entries,
    _build_row,
    _ScenarioEntry,
    compare_candidate_backend_results,
)

_MEANS = st.floats(
    min_value=1e-6,
    max_value=1e6,
    allow_nan=False,
    allow_infinity=False,
)
_NAMES = st.text(alphabet="abcdef0123", min_size=1, max_size=6)
_GROUPS = st.dictionaries(_NAMES, st.tuples(_MEANS, _MEANS), max_size=6)


@given(python_mean=_MEANS, rust_mean=_MEANS, comparison_id=_NAMES)
def test_build_row_ratio_and_trichotomy(
    python_mean: float,
    rust_mean: float,
    comparison_id: str,
) -> None:
    """A row's winner label agrees with its ratio.

    These invariants must hold regardless of the tolerance or comparison
    strategy ``_build_row`` uses internally, so they cannot pass by sharing a
    misconception with the implementation.
    """
    row = _build_row(
        comparison_id=comparison_id,
        python_entry=_ScenarioEntry(scenario_name="py", mean=python_mean),
        rust_entry=_ScenarioEntry(scenario_name="rs", mean=rust_mean),
    )
    assert row.speedup_ratio == python_mean / rust_mean, (
        "speedup_ratio must equal python_mean / rust_mean"
    )
    assert row.faster_backend in {"tie", "rust", "python"}, (
        f"faster_backend must be a known label, got {row.faster_backend!r}"
    )

    # Winning implies a directional ratio: rust wins only when python is
    # slower (ratio > 1), python wins only when python is faster (ratio < 1).
    if row.faster_backend == "rust":
        assert row.speedup_ratio > 1, "rust winning must imply speedup_ratio > 1"
    if row.faster_backend == "python":
        assert row.speedup_ratio < 1, "python winning must imply speedup_ratio < 1"

    # A tie must imply the ratio is approximately 1, whatever tolerance the
    # implementation applies to reach that conclusion.
    if row.faster_backend == "tie":
        assert math.isclose(row.speedup_ratio, 1.0, rel_tol=1e-6, abs_tol=1e-6), (
            "a tie must imply speedup_ratio is approximately 1"
        )

    # Antisymmetry: swapping which mean is called "python" and which is
    # "rust" must invert the ratio and swap the winner (a tie stays a tie).
    swapped = _build_row(
        comparison_id=comparison_id,
        python_entry=_ScenarioEntry(scenario_name="rs", mean=rust_mean),
        rust_entry=_ScenarioEntry(scenario_name="py", mean=python_mean),
    )
    assert math.isclose(row.speedup_ratio * swapped.speedup_ratio, 1.0, rel_tol=1e-6), (
        "swapping the two means must invert speedup_ratio"
    )
    swapped_winner = {"rust": "python", "python": "rust", "tie": "tie"}[
        row.faster_backend
    ]
    assert swapped.faster_backend == swapped_winner, (
        "swapping the two means must swap which backend is reported faster"
    )


@given(groups=_GROUPS)
def test_report_is_sorted_and_tally_partitions_rows(
    groups: dict[str, tuple[float, float]],
) -> None:
    """The report sorts rows by id and its tally partitions them exactly."""
    grouped = {
        comparison_id: {
            "python": _ScenarioEntry(scenario_name=f"{comparison_id}-py", mean=pm),
            "rust": _ScenarioEntry(scenario_name=f"{comparison_id}-rs", mean=rm),
        }
        for comparison_id, (pm, rm) in groups.items()
    }
    report = _build_report_from_grouped_entries(grouped)

    ids = [row.comparison_id for row in report.rows]
    # Comparing against the sorted group keys pins both ordering and the exact
    # set of ids: exactly one row per input group, none omitted or duplicated.
    assert ids == sorted(grouped), "rows must be one-per-group, ordered by id"
    assert report.summary.row_count == len(grouped) == len(report.rows), (
        "row_count must equal the number of groups and emitted rows"
    )

    tally = Counter(row.faster_backend for row in report.rows)
    assert report.summary.rust_wins == tally["rust"], "rust_wins must match the tally"
    assert report.summary.python_wins == tally["python"], (
        "python_wins must match the tally"
    )
    assert report.summary.ties == tally["tie"], "ties must match the tally"
    assert (
        report.summary.rust_wins + report.summary.python_wins + report.summary.ties
        == report.summary.row_count
    ), "wins and ties must partition every row exactly"


@pytest.mark.parametrize(
    ("present_backend", "missing_pattern"),
    [
        ("python", "missing Rust scenario"),
        ("rust", "missing Python scenario"),
    ],
)
@given(comparison_id=_NAMES, mean=_MEANS)
def test_group_missing_a_backend_is_rejected(
    comparison_id: str,
    mean: float,
    present_backend: str,
    missing_pattern: str,
) -> None:
    """A group lacking either backend raises ``ValueError`` for that backend."""
    grouped = {
        comparison_id: {
            present_backend: _ScenarioEntry(scenario_name=present_backend, mean=mean),
        },
    }
    with pytest.raises(ValueError, match=missing_pattern):
        _build_report_from_grouped_entries(grouped)


def _candidate_payloads(
    scenarios: list[dict[str, object]],
    means: list[float],
) -> tuple[dict[str, object], dict[str, object]]:
    """Build matching plan and throughput payloads for the given scenarios."""
    plan = {"scenarios": scenarios}
    throughput = {"results": [{"mean": mean} for mean in means]}
    return plan, throughput


def test_duplicate_backend_entries_are_rejected() -> None:
    """Two entries for the same scenario and backend raise ``ValueError``."""
    scenarios: list[dict[str, object]] = [
        {"name": "python-scenario", "backend": "python"},
        {"name": "python-scenario", "backend": "python"},
    ]
    plan, throughput = _candidate_payloads(scenarios, [1.0, 2.0])
    with pytest.raises(ValueError, match="duplicate"):
        compare_candidate_backend_results(
            plan_payload=plan,
            throughput_payload=throughput,
        )


def test_plan_result_count_mismatch_is_rejected() -> None:
    """A plan/result length mismatch raises ``ValueError`` before pairing."""
    scenarios: list[dict[str, object]] = [
        {"name": "python-scenario", "backend": "python"},
    ]
    plan, throughput = _candidate_payloads(scenarios, [])
    with pytest.raises(ValueError, match="must match"):
        compare_candidate_backend_results(
            plan_payload=plan,
            throughput_payload=throughput,
        )
