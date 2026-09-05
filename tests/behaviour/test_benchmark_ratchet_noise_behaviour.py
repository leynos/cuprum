"""Behavioural tests for the ratchet's tolerance of benchmark noise.

Stated as measurements rather than as statistics: the numbers in the feature
file are the ones from the 2026-08-06 incident, so a reader can check the
scenarios against what actually happened rather than against a description
of the median.
"""

from __future__ import annotations

import json
import typing as typ

from pytest_bdd import given, parsers, scenario, then, when

from benchmarks.benchmark_profile import BENCHMARK_PROFILE_VERSION
from benchmarks.ratchet_history import (
    BaselineHistory,
    HistorySample,
    write_history,
)
from benchmarks.ratchet_rust_performance import main as ratchet_cli
from tests.behaviour._benchmark_ratchet_support import _write_json

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

FEATURE = "../features/benchmark_ratchet_noise.feature"
SCENARIO = "medium-single-nocb"
WORKER_ITERATIONS = 20


@scenario(FEATURE, "One anomalous main run does not fail the pull requests after it")
def test_one_anomalous_main_run_does_not_fail_later_pull_requests() -> None:
    """One anomalous main run does not fail the pull requests after it."""


@scenario(FEATURE, "The same pull request fails against that run alone")
def test_the_same_pull_request_fails_against_that_run_alone() -> None:
    """The same pull request fails against that run alone."""


@scenario(FEATURE, "A genuine slowdown fails against a settled window")
def test_a_genuine_slowdown_fails_against_a_settled_window() -> None:
    """A genuine slowdown fails against a settled window."""


@scenario(FEATURE, "A slowdown fails even when the window is noisy")
def test_a_slowdown_fails_even_when_the_window_is_noisy() -> None:
    """A slowdown fails even when the window is noisy."""


@scenario(FEATURE, "Measuring what main measures is never a regression")
def test_measuring_what_main_measures_is_never_a_regression() -> None:
    """Measuring what main measures is never a regression."""


@scenario(FEATURE, "A missing baseline skips the ratchet with durable evidence")
def test_a_missing_baseline_skips_the_ratchet_with_durable_evidence() -> None:
    """A missing baseline skips the ratchet with durable evidence."""


def _sample(ratio: float, *, run_id: str) -> HistorySample:
    """Return one recorded main-branch measurement."""
    return HistorySample(
        commit=f"commit-{run_id}",
        run_id=run_id,
        benchmark_profile_version=BENCHMARK_PROFILE_VERSION,
        worker_iterations=WORKER_ITERATIONS,
        ratios={SCENARIO: ratio},
    )


def _parse_ratios(measurements: str) -> tuple[float, ...]:
    """Parse the ``a, b and c`` measurement list a scenario states."""
    return tuple(
        float(value.strip())
        for value in measurements.replace(" and ", ", ").split(",")
        if value.strip()
    )


def _candidate_payloads(ratio: float) -> tuple[dict[str, object], dict[str, object]]:
    """Return plan and throughput payloads for one pull-request measurement.

    The Python mean is fixed at one second so the Rust mean carries the
    ratio the ratchet reads.

    Returns
    -------
    tuple[dict[str, object], dict[str, object]]
        Candidate plan and throughput payloads with logical command names.
    """
    return (
        {
            "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
            "worker_iterations": WORKER_ITERATIONS,
            "scenarios": [
                {"name": f"python-{SCENARIO}", "backend": "python"},
                {"name": f"rust-{SCENARIO}", "backend": "rust"},
            ],
        },
        {
            "results": [
                {"command": f"python-{SCENARIO}", "mean": 1.0},
                {"command": f"rust-{SCENARIO}", "mean": ratio},
            ],
        },
    )


# -- Given steps ---------------------------------------------------------------


@given(
    parsers.parse("main has measured {measurements}"),
    target_fixture="history",
)
def given_main_has_measured(measurements: str) -> BaselineHistory:
    """Record the stated main-branch measurements, oldest first."""
    return BaselineHistory(
        samples=tuple(
            _sample(ratio, run_id=str(index))
            for index, ratio in enumerate(_parse_ratios(measurements))
        )
    )


@given(
    parsers.parse("a noisy main run then measured {measurement:f}"),
    target_fixture="history",
)
def given_a_noisy_main_run(
    history: BaselineHistory, measurement: float
) -> BaselineHistory:
    """Append the anomalous measurement, as the workflow would."""
    return history.appended(_sample(measurement, run_id="noisy"))


@given("no main baseline is available", target_fixture="history")
def given_no_main_baseline_is_available() -> None:
    """Represent a first ratchet run with no downloaded baseline artefact."""


# -- When steps ----------------------------------------------------------------


@when(
    parsers.parse("a pull request measures {measurement:f}"),
    target_fixture="verdict",
)
def when_a_pull_request_measures(
    history: BaselineHistory | None, measurement: float, tmp_path: pth.Path
) -> tuple[int, cabc.Mapping[str, object]]:
    """Judge the pull request through the ratchet CLI and persisted report."""
    candidate_plan, candidate_throughput = _candidate_payloads(measurement)
    candidate_plan_path = tmp_path / "candidate-plan.json"
    candidate_throughput_path = tmp_path / "candidate-throughput.json"
    output_path = tmp_path / "ratchet-report.json"
    _write_json(path=candidate_plan_path, payload=candidate_plan)
    _write_json(path=candidate_throughput_path, payload=candidate_throughput)
    argv = [
        "--candidate-plan",
        str(candidate_plan_path),
        "--candidate-throughput",
        str(candidate_throughput_path),
        "--max-regression",
        "0.30",
        "--output",
        str(output_path),
    ]
    if history is not None:
        history_path = tmp_path / "main-baseline-history.json"
        write_history(history=history, output_path=history_path)
        argv.extend(("--baseline-history", str(history_path)))

    exit_code = ratchet_cli(argv)
    verdict = typ.cast("dict[str, object]", json.loads(output_path.read_text()))

    return exit_code, verdict


# -- Then steps ----------------------------------------------------------------


def _assert_ratchet_verdict(
    verdict: tuple[int, cabc.Mapping[str, object]], passed: bool
) -> None:
    """Assert the ratchet CLI status and persisted verdict agree."""
    exit_code, report = verdict
    assert exit_code == int(not report["passed"]), (
        "the ratchet CLI exit code must match its persisted passed verdict"
    )
    assert report["passed"] is passed, (
        f"expected the ratchet to {'pass' if passed else 'fail'}; report was {report}"
    )
    if passed:
        assert report["regressions"] == [], (
            "a passing report must not retain any regression entries"
        )
    else:
        assert report["regressions"], "a failed report must retain a regression entry"


@then("the ratchet passes")
def then_the_ratchet_passes(
    verdict: tuple[int, cabc.Mapping[str, object]],
) -> None:
    """Assert the comparison reported no regression."""
    _assert_ratchet_verdict(verdict, passed=True)


@then("the ratchet fails")
def then_the_ratchet_fails(
    verdict: tuple[int, cabc.Mapping[str, object]],
) -> None:
    """Assert the comparison reported a regression."""
    _assert_ratchet_verdict(verdict, passed=False)


@then("the ratchet records history-backed comparison evidence")
def then_the_ratchet_records_history_backed_comparison_evidence(
    verdict: tuple[int, cabc.Mapping[str, object]],
    history: BaselineHistory,
) -> None:
    """Assert a history-backed report records its durable decision evidence."""
    _exit_code, report = verdict
    assert report["baseline_source"] == "history", (
        "the behavioural window must be selected from recorded history"
    )
    assert report["baseline_reason"] == "compatible_history", (
        "the compatible history selection must be durable evidence"
    )
    assert report["compatible_sample_count"] == len(history.samples), (
        "the report must retain every compatible history sample in this scenario"
    )
    assert report["comparison_state"] == "compared", (
        "history-backed behavioural scenarios must perform a comparison"
    )


@then("the ratchet is skipped with no-baseline evidence")
def then_the_ratchet_is_skipped_with_no_baseline_evidence(
    verdict: tuple[int, cabc.Mapping[str, object]],
) -> None:
    """Assert the skipped first-run report remains machine-readable evidence."""
    exit_code, report = verdict
    assert exit_code == int(not report["passed"]), (
        "the ratchet CLI exit code must match its persisted passed verdict"
    )
    assert report["passed"] is True, "a missing baseline must intentionally pass"
    assert report["comparison_performed"] is False, (
        "a missing baseline must not claim that it performed a comparison"
    )
    assert report["baseline_source"] == "none", (
        "a first run must record that no baseline source was selected"
    )
    assert report["baseline_reason"] == "no_baseline_available", (
        "a first run must retain its bounded no-baseline reason"
    )
    assert report["compatible_sample_count"] == 0, (
        "a first run cannot have compatible history samples"
    )
    assert report["comparison_state"] == "skipped_no_baseline", (
        "a first run must persist its intentional skipped comparison"
    )
