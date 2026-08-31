"""Behavioural tests for the ratchet's tolerance of benchmark noise.

Stated as measurements rather than as statistics: the numbers in the feature
file are the ones from the 2026-08-06 incident, so a reader can check the
scenarios against what actually happened rather than against a description
of the median.
"""

from __future__ import annotations

import typing as typ

from pytest_bdd import given, parsers, scenario, then, when

from benchmarks.benchmark_profile import BENCHMARK_PROFILE_VERSION
from benchmarks.ratchet_history import (
    BaselineHistory,
    HistorySample,
    RatchetPolicy,
)
from benchmarks.ratchet_rust_performance import compare_rust_regressions
from benchmarks.ratchet_types import BenchmarkRunPayload

if typ.TYPE_CHECKING:
    import collections.abc as cabc

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


def _candidate(ratio: float) -> BenchmarkRunPayload:
    """Return a pull-request run measuring one Rust/Python ratio.

    The Python mean is fixed at one second so the Rust mean carries the
    ratio the ratchet reads.

    Returns
    -------
    BenchmarkRunPayload
        A candidate measurement with logical Hyperfine command names.
    """
    return BenchmarkRunPayload(
        plan={
            "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
            "worker_iterations": WORKER_ITERATIONS,
            "scenarios": [
                {"name": f"python-{SCENARIO}", "backend": "python"},
                {"name": f"rust-{SCENARIO}", "backend": "rust"},
            ],
        },
        throughput={
            "results": [
                {"command": f"python-{SCENARIO}", "mean": 1.0},
                {"command": f"rust-{SCENARIO}", "mean": ratio},
            ],
        },
        context_name="candidate",
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


# -- When steps ----------------------------------------------------------------


@when(
    parsers.parse("a pull request measures {measurement:f}"),
    target_fixture="verdict",
)
def when_a_pull_request_measures(
    history: BaselineHistory, measurement: float
) -> cabc.Mapping[str, object]:
    """Judge the pull request's measurement against the window."""
    report = compare_rust_regressions(
        candidate=_candidate(measurement),
        history=history,
        policy=RatchetPolicy(max_regression=0.30),
    )
    return report.as_dict()


# -- Then steps ----------------------------------------------------------------


@then("the ratchet passes")
def then_the_ratchet_passes(verdict: cabc.Mapping[str, object]) -> None:
    """Assert the comparison reported no regression."""
    assert verdict["passed"] is True, (
        f"expected the ratchet to pass; report was {verdict}"
    )


@then("the ratchet fails")
def then_the_ratchet_fails(verdict: cabc.Mapping[str, object]) -> None:
    """Assert the comparison reported a regression."""
    assert verdict["passed"] is False, (
        f"expected the ratchet to fail; report was {verdict}"
    )
