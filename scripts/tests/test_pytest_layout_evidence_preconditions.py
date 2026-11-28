"""Tests for the evidence preconditions the adoption rule enforces.

A median is only evidence when the sample is the one the plan predeclared:
warm pairs on the same interpreter leg, on comparable runners and toolchains,
over runs that collected the same suite, at no extra whole-job cost. These
tests drive each precondition separately, so a rule that quietly dropped one
would fail here rather than at the moment it approved a change.
"""

from __future__ import annotations

import dataclasses

from scripts.pytest_layout_evidence import MINIMUM_PAIRS, RunRecord, compare
from scripts.tests.test_pytest_layout_evidence import _run, _Spec


def _warm_pairs(count: int, *, seconds: float = 100.0) -> list[RunRecord]:
    """Return ``count`` warm, passing pairs with a fixed control time."""
    records: list[RunRecord] = []
    for index in range(count):
        records.extend((
            _run(f"c{index}", "baseline", _Spec(seconds=seconds)),
            _run(f"n{index}", "consolidated", _Spec(seconds=seconds - 40.0)),
        ))
    return records


def test_a_thin_interpreter_leg_is_inconclusive() -> None:
    """Refuse a median when one interpreter leg has too few warm pairs.

    The plan requires five warm pairs per interpreter, so a healthy total that
    is concentrated on one leg is not the predeclared sample.
    """
    records = _warm_pairs(MINIMUM_PAIRS)
    records.extend((
        dataclasses.replace(
            _run("c-14", "baseline", _Spec(seconds=100.0)), python_version="3.14"
        ),
        dataclasses.replace(
            _run("n-14", "consolidated", _Spec(seconds=60.0)), python_version="3.14"
        ),
    ))

    comparison = compare(records)

    assert comparison.state == "inconclusive", comparison.reason
    assert "3.14" in comparison.reason, (
        f"the thin leg must be named, got {comparison.reason}"
    )


def test_a_runner_mismatch_is_inconclusive() -> None:
    """Refuse a pair that ran on a different runner or toolchain."""
    records = _warm_pairs(MINIMUM_PAIRS)
    records[-1] = dataclasses.replace(records[-1], runner_label="other-runner")

    comparison = compare(records)

    assert comparison.state == "inconclusive", comparison.reason
    assert "different runners" in comparison.reason, (
        f"the mismatch must be named, got {comparison.reason}"
    )


def test_a_collection_mismatch_is_inconclusive() -> None:
    """Refuse a pair that collected a different suite."""
    records = _warm_pairs(MINIMUM_PAIRS)
    records[-1] = dataclasses.replace(records[-1], node_ids=("only-candidate",))

    comparison = compare(records)

    assert comparison.state == "inconclusive", comparison.reason
    assert "different suites" in comparison.reason, (
        f"the collection mismatch must be named, got {comparison.reason}"
    )


def test_a_job_cost_increase_is_inconclusive() -> None:
    """Refuse an arm whose whole-job seconds rose, however good the step."""
    records = _warm_pairs(MINIMUM_PAIRS)
    records[-1] = dataclasses.replace(records[-1], job_seconds=10_000.0)

    comparison = compare(records)

    assert comparison.state == "inconclusive", comparison.reason
    assert "whole-job seconds" in comparison.reason, (
        f"the cost increase must be named, got {comparison.reason}"
    )


def test_an_expensive_unpaired_failure_blocks_adoption() -> None:
    """Refuse adoption when an unpaired run's cost is not accounted for.

    Five qualifying pairs can look like a healthy sample while an expensive
    unpaired failure sits outside it. That work was billed, so it counts.
    """
    records = _warm_pairs(MINIMUM_PAIRS)
    records.append(
        dataclasses.replace(
            _run("n-expensive", "consolidated", _Spec(seconds=90.0)),
            job_seconds=50_000.0,
            exit_status=1,
            failed=True,
        )
    )

    comparison = compare(records)

    assert comparison.state == "inconclusive", comparison.reason
    assert "whole-job seconds" in comparison.reason, (
        f"the unaccounted cost must be named, got {comparison.reason}"
    )


def test_pairs_without_recorded_outcomes_are_inconclusive() -> None:
    """Refuse a verdict when no executed outcomes were recorded.

    Two empty outcome mappings agree trivially. If the runs collected nodes and
    neither side recorded what happened to them, that agreement is not evidence.
    """
    records = _warm_pairs(MINIMUM_PAIRS)
    records = [dataclasses.replace(record, outcomes=()) for record in records]

    comparison = compare(records)

    assert comparison.state == "inconclusive", comparison.reason
    assert "no executed outcomes" in comparison.reason, (
        f"the missing evidence must be named, got {comparison.reason}"
    )
