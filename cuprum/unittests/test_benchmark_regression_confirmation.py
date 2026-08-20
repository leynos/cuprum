"""Unit tests for confirming a reported regression by measuring again.

The window makes the *bar* robust to one noisy run; it cannot make the
*candidate* robust. A pull request measured once on an unlucky runner still
reports whatever that runner produced, and before this the only recourse was
a human pressing re-run. Re-measuring and intersecting the two verdicts
means a flake has to land on the same scenario twice to survive.

The asymmetries matter more than the happy path. Confirmation may only turn
a failure into a pass — a second chance to fail would double the false
failures it exists to halve — and a confirmation that could not compare at
all leaves the first verdict standing rather than waving it through.
"""

from __future__ import annotations

import json
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from benchmarks.comparison_report import load_ratchet_report
from benchmarks.confirm_regression import confirm_regressions
from benchmarks.confirm_regression import main as confirm_cli

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

SCENARIOS = ("small-single-nocb", "medium-single-nocb", "medium-single-cb")


def _comparison(scenario: str, *, regressed: bool) -> dict[str, object]:
    """Return one scenario comparison entry as the ratchet writes it."""
    regression_ratio = 0.9 if regressed else 0.01
    return {
        "scenario_name": scenario,
        "baseline_ratio": 1.0,
        "baseline_sample_count": 7,
        "candidate_ratio": 1.0 + regression_ratio,
        "regression_ratio": regression_ratio,
        "max_regression": 0.30,
        "noise_tolerance": 0.0,
        "effective_threshold": 0.30,
        "is_regression": regressed,
    }


def _report(*regressed: str) -> dict[str, object]:
    """Return a ratchet report flagging the named scenarios."""
    comparisons = [
        _comparison(scenario, regressed=scenario in regressed) for scenario in SCENARIOS
    ]
    return {
        "max_regression": 0.30,
        "baseline_sample_count": 7,
        "passed": not regressed,
        "rust_scenarios_compared": len(comparisons),
        "worst_regression_ratio": 0.9 if regressed else 0.01,
        "comparisons": comparisons,
        "regressions": [
            comparison
            for comparison in comparisons
            if comparison["scenario_name"] in regressed
        ],
    }


def _skip_report() -> dict[str, object]:
    """Return the report a run writes when it could not compare at all."""
    return {
        "baseline_available": True,
        "comparison_performed": False,
        "passed": True,
        "reason": "incompatible_benchmark_profile",
    }


def _names(entries: object) -> list[str]:
    """Return the scenario names of a list of comparison entries."""
    return [
        str(entry["scenario_name"])
        for entry in typ.cast("list[dict[str, object]]", entries)
    ]


def test_a_regression_that_reproduces_fails() -> None:
    """The same scenario flagged twice is signal, not noise."""
    combined = confirm_regressions(
        primary=_report("medium-single-nocb"),
        confirmation=_report("medium-single-nocb"),
    )

    assert combined["passed"] is False
    assert _names(combined["confirmed_regressions"]) == ["medium-single-nocb"]
    assert combined["unconfirmed_regressions"] == []


def test_a_regression_that_does_not_reproduce_passes() -> None:
    """One unlucky measurement must not fail a pull request on its own."""
    combined = confirm_regressions(
        primary=_report("medium-single-nocb"),
        confirmation=_report(),
    )

    assert combined["passed"] is True
    assert _names(combined["unconfirmed_regressions"]) == ["medium-single-nocb"]
    assert combined["confirmed_regressions"] == []


def test_a_flake_that_moves_scenario_does_not_confirm() -> None:
    """Confirmation is per scenario, not "something regressed twice".

    Two different scenarios failing one measurement each is exactly what
    runner noise looks like; treating it as confirmation would defeat the
    re-measurement.
    """
    combined = confirm_regressions(
        primary=_report("medium-single-nocb"),
        confirmation=_report("small-single-nocb"),
    )

    assert combined["passed"] is True
    assert combined["confirmed_regressions"] == []


def test_the_confirmation_cannot_introduce_a_new_failure() -> None:
    """A scenario the first run did not flag is not failed by the second.

    Otherwise re-measuring would be a second chance to fail, doubling the
    false-failure rate it exists to halve.
    """
    combined = confirm_regressions(
        primary=_report(),
        confirmation=_report("medium-single-nocb", "small-single-nocb"),
    )

    assert combined["passed"] is True
    assert combined["confirmed_regressions"] == []


def test_an_unusable_confirmation_leaves_the_first_verdict_standing() -> None:
    """Failing closed: a broken retry is not evidence about the candidate.

    The primary comparison succeeded on the same inputs, so a confirmation
    that could not compare says something went wrong with the retry — not
    that the regression was noise.
    """
    combined = confirm_regressions(
        primary=_report("medium-single-nocb"),
        confirmation=_skip_report(),
    )

    assert combined["passed"] is False
    assert _names(combined["confirmed_regressions"]) == ["medium-single-nocb"]


def test_the_combined_report_keeps_the_shape_its_consumers_read() -> None:
    """The workflow summary reads this file; it must stay readable.

    `load_ratchet_report` is the real consumer, so the check is that it
    reports the combined verdict rather than that particular keys exist.
    """
    combined = confirm_regressions(
        primary=_report("medium-single-nocb"),
        confirmation=_report(),
    )

    assert combined["rust_scenarios_compared"] == len(SCENARIOS)
    assert _names(combined["primary_regressions"]) == ["medium-single-nocb"]


@pytest.mark.parametrize(
    ("confirmation_regressions", "expected_exit", "expected_status"),
    [
        pytest.param(("medium-single-nocb",), 1, "failed", id="reproduced"),
        pytest.param((), 0, "passed", id="not-reproduced"),
    ],
)
def test_the_cli_writes_a_report_the_summary_can_read(
    tmp_path: pth.Path,
    confirmation_regressions: tuple[str, ...],
    expected_exit: int,
    expected_status: str,
) -> None:
    """End to end: two reports in, one verdict out, exit code to match."""
    primary = tmp_path / "ratchet-report-primary.json"
    confirmation = tmp_path / "ratchet-report-confirmation.json"
    output = tmp_path / "ratchet-report.json"
    primary.write_text(json.dumps(_report("medium-single-nocb")), encoding="utf-8")
    confirmation.write_text(
        json.dumps(_report(*confirmation_regressions)), encoding="utf-8"
    )

    exit_code = confirm_cli([
        "--primary-report",
        str(primary),
        "--confirmation-report",
        str(confirmation),
        "--output",
        str(output),
    ])

    assert exit_code == expected_exit
    assert load_ratchet_report(output).status == expected_status


def test_the_cli_reports_malformed_input_without_passing_it(
    tmp_path: pth.Path,
) -> None:
    """An unreadable report must not be read as "no regressions"."""
    primary = tmp_path / "primary.json"
    confirmation = tmp_path / "confirmation.json"
    primary.write_text("{ not json", encoding="utf-8")
    confirmation.write_text(json.dumps(_report()), encoding="utf-8")

    exit_code = confirm_cli([
        "--primary-report",
        str(primary),
        "--confirmation-report",
        str(confirmation),
        "--output",
        str(tmp_path / "out.json"),
    ])

    assert exit_code == 2


_SCENARIO_SETS = st.frozensets(st.sampled_from(SCENARIOS), max_size=len(SCENARIOS))


@given(primary=_SCENARIO_SETS, confirmation=_SCENARIO_SETS)
def test_confirmation_only_ever_narrows_the_failure(
    primary: cabc.Set[str], confirmation: cabc.Set[str]
) -> None:
    """Whatever the second run measures, it cannot widen the first verdict."""
    combined = confirm_regressions(
        primary=_report(*primary),
        confirmation=_report(*confirmation),
    )

    assert set(_names(combined["confirmed_regressions"])) <= set(primary)
    if not primary:
        assert combined["passed"] is True


@given(regressed=_SCENARIO_SETS)
def test_a_reproduced_verdict_is_the_primary_verdict(
    regressed: cabc.Set[str],
) -> None:
    """Measuring the same thing twice must decide the same way.

    A confirmation identical to the primary is the case where the extra
    measurement adds nothing; the verdict must then be untouched by it.
    """
    report = _report(*regressed)
    combined = confirm_regressions(primary=report, confirmation=report)

    assert combined["passed"] == report["passed"]
    assert set(_names(combined["confirmed_regressions"])) == set(regressed)
