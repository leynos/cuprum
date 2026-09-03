"""Unit tests for benchmark CI ratchet comparison helpers."""

from __future__ import annotations

import typing as typ

import pytest

from benchmarks.benchmark_profile import BENCHMARK_PROFILE_VERSION
from benchmarks.ratchet_history import BaselineHistory, HistorySample, RatchetPolicy
from benchmarks.ratchet_rust_performance import (
    BenchmarkRunPayload,
    ComparisonReport,
    compare_rust_regressions,
)


def _scenario_payload(*, name: str, backend: str) -> dict[str, object]:
    """Return a scenario payload dict."""
    return {
        "name": name,
        "backend": backend,
        "payload_bytes": 1024,
        "stages": 2,
        "with_line_callbacks": False,
    }


def _plan_payload() -> dict[str, object]:
    """Return a benchmark plan payload."""
    return {
        "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
        "dry_run": True,
        "rust_available": True,
        "worker_iterations": 20,
        "command": ["hyperfine", "placeholder"],
        "scenarios": [
            _scenario_payload(name="python-small-single-nocb", backend="python"),
            _scenario_payload(name="rust-small-single-nocb", backend="rust"),
        ],
    }


def _throughput_payload(*, python_mean: float, rust_mean: float) -> dict[str, object]:
    """Return a throughput payload."""
    return {
        "results": [
            {"command": "python-small-single-nocb", "mean": python_mean},
            {"command": "rust-small-single-nocb", "mean": rust_mean},
        ],
    }


class _RunMeans(typ.NamedTuple):
    """Python and Rust mean runtimes for one benchmark run."""

    python: float
    rust: float


def _run_comparison(
    *,
    baseline: _RunMeans,
    candidate: _RunMeans,
    max_regression: float = 0.10,
) -> ComparisonReport:
    """Build baseline/candidate payloads and run the Rust ratchet comparison."""
    return compare_rust_regressions(
        baseline=BenchmarkRunPayload(
            plan=_plan_payload(),
            throughput=_throughput_payload(
                python_mean=baseline.python,
                rust_mean=baseline.rust,
            ),
            context_name="baseline",
        ),
        candidate=BenchmarkRunPayload(
            plan=_plan_payload(),
            throughput=_throughput_payload(
                python_mean=candidate.python,
                rust_mean=candidate.rust,
            ),
            context_name="candidate",
        ),
        policy=RatchetPolicy(max_regression=max_regression),
    )


def _comparison_with_options(**options: object) -> ComparisonReport:
    """Compare two simple runs through the public options boundary."""
    return compare_rust_regressions(
        baseline=BenchmarkRunPayload(
            plan=_plan_payload(),
            throughput=_throughput_payload(python_mean=1.0, rust_mean=1.0),
            context_name="baseline",
        ),
        candidate=BenchmarkRunPayload(
            plan=_plan_payload(),
            throughput=_throughput_payload(python_mean=1.0, rust_mean=1.1),
            context_name="candidate",
        ),
        **typ.cast("typ.Any", options),
    )


def _history(*ratios: float) -> BaselineHistory:
    """Build compatible main-branch history samples for policy tests."""
    return BaselineHistory(
        samples=tuple(
            HistorySample(
                commit=str(index),
                run_id=str(index),
                benchmark_profile_version=BENCHMARK_PROFILE_VERSION,
                worker_iterations=20,
                ratios={"small-single-nocb": ratio},
            )
            for index, ratio in enumerate(ratios)
        )
    )


def test_compare_rust_regressions_passes_within_threshold() -> None:
    """A Rust/Python ratio increase at or under 10% should pass the ratchet."""
    report = _run_comparison(
        baseline=_RunMeans(python=0.50, rust=1.00),
        candidate=_RunMeans(python=0.50, rust=1.10),
    )

    assert report.passed is True
    assert report.rust_scenarios_compared == 1
    assert len(report.comparisons) == 1
    assert report.comparisons[0].scenario_name == "small-single-nocb"
    assert report.comparisons[0].baseline_ratio == pytest.approx(2.0)
    assert report.comparisons[0].candidate_ratio == pytest.approx(2.2)
    assert report.comparisons[0].regression_ratio == pytest.approx(0.10)


def test_compare_rust_regressions_rejects_unsupported_policy_option() -> None:
    """Only the policy and legacy threshold options are accepted."""
    with pytest.raises(TypeError, match="unsupported comparison option\\(s\\): extra"):
        _comparison_with_options(extra=0.30)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        pytest.param(
            {"policy": object()}, "policy must be a RatchetPolicy", id="policy"
        ),
        pytest.param(
            {"max_regression": 1}, "max_regression must be a float", id="integer"
        ),
    ],
)
def test_compare_rust_regressions_rejects_invalid_policy_options(
    options: dict[str, object], message: str
) -> None:
    """Policy options are type-checked through the public comparison API."""
    with pytest.raises(TypeError, match=message):
        _comparison_with_options(**options)


def test_compare_rust_regressions_rejects_simultaneous_policy_and_legacy_option() -> (
    None
):
    """The two policy configurations must not silently disagree."""
    with pytest.raises(
        ValueError, match="pass either policy or max_regression, not both"
    ):
        _comparison_with_options(
            policy=RatchetPolicy(max_regression=0.30), max_regression=0.30
        )


def test_compare_rust_regressions_honours_the_legacy_threshold_option() -> None:
    """The legacy threshold option continues to select the report threshold."""
    report = _comparison_with_options(max_regression=0.20)

    assert report.comparisons[0].max_regression == pytest.approx(0.20), (
        "the legacy max_regression option must construct the requested policy"
    )


def test_compare_rust_regressions_preserves_a_supplied_policy() -> None:
    """A supplied policy keeps its threshold, noise scale, and window size."""
    policy = RatchetPolicy(max_regression=0.10, noise_sigmas=0.50, window_size=2)
    report = compare_rust_regressions(
        baseline=BenchmarkRunPayload(
            plan=_plan_payload(),
            throughput=_throughput_payload(python_mean=1.0, rust_mean=1.0),
            context_name="baseline",
        ),
        candidate=BenchmarkRunPayload(
            plan=_plan_payload(),
            throughput=_throughput_payload(python_mean=1.0, rust_mean=1.1),
            context_name="candidate",
        ),
        history=_history(1.0, 3.0, 4.0),
        policy=policy,
    )
    comparison = report.comparisons[0]

    assert comparison.max_regression == pytest.approx(policy.max_regression), (
        "the supplied policy's flat threshold must reach the comparison"
    )
    assert comparison.baseline_sample_count == policy.window_size, (
        "the supplied policy's window size must limit history samples"
    )
    assert comparison.noise_tolerance == pytest.approx(0.1059), (
        "the supplied policy's noise scale must determine the MAD band"
    )


def test_compare_rust_regressions_pairs_command_names_with_raw_commands() -> None:
    """Plans with raw worker commands pair with logical Hyperfine names.

    The CI plan ``command`` vector carries the raw shell worker commands that
    Hyperfine runs, while the v4 profile adds ``--command-name`` options so
    the throughput JSON ``results[*].command`` fields expose the logical
    scenario names. The ratchet must match those logical names against each
    plan's ``scenarios[*].name`` without ever parsing the raw commands.
    """
    raw_command_plan: dict[str, object] = {
        "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
        "dry_run": True,
        "rust_available": True,
        "worker_iterations": 20,
        "command": [
            "hyperfine",
            "--export-json",
            "throughput.json",
            "--warmup",
            "1",
            "--runs",
            "10",
            "--command-name",
            "python-small-single-nocb",
            "--command-name",
            "rust-small-single-nocb",
            "CUPRUM_STREAM_BACKEND=python ...",
            "CUPRUM_STREAM_BACKEND=rust ...",
        ],
        "scenarios": [
            _scenario_payload(name="python-small-single-nocb", backend="python"),
            _scenario_payload(name="rust-small-single-nocb", backend="rust"),
        ],
    }

    report = compare_rust_regressions(
        baseline=BenchmarkRunPayload(
            plan=raw_command_plan,
            throughput=_throughput_payload(python_mean=1.00, rust_mean=2.00),
            context_name="baseline",
        ),
        candidate=BenchmarkRunPayload(
            plan=raw_command_plan,
            throughput=_throughput_payload(python_mean=1.00, rust_mean=2.00),
            context_name="candidate",
        ),
        max_regression=0.10,
    )

    assert report.passed is True
    assert report.rust_scenarios_compared == 1
    assert report.comparisons[0].scenario_name == "small-single-nocb"
    assert report.comparisons[0].baseline_ratio == pytest.approx(2.0)
    assert report.comparisons[0].candidate_ratio == pytest.approx(2.0)
    assert report.comparisons[0].regression_ratio == pytest.approx(0.0)


def test_compare_rust_regressions_rejects_mismatched_result_command() -> None:
    """The comparison must validate result commands through its public path."""
    with pytest.raises(ValueError, match=r"results\[0\]\.command .* must match"):
        compare_rust_regressions(
            baseline=BenchmarkRunPayload(
                plan=_plan_payload(),
                throughput=_throughput_payload(python_mean=1.0, rust_mean=1.0),
                context_name="baseline",
            ),
            candidate=BenchmarkRunPayload(
                plan=_plan_payload(),
                throughput={
                    "results": [
                        {"command": "rust-small-single-nocb", "mean": 1.0},
                        {"command": "rust-small-single-nocb", "mean": 1.0},
                    ],
                },
                context_name="candidate",
            ),
            policy=RatchetPolicy(max_regression=0.10),
        )


def test_compare_rust_regressions_ignores_runner_speed_differences() -> None:
    """Uniform runner slowdowns cancel out of the within-run ratio."""
    report = _run_comparison(
        baseline=_RunMeans(python=0.50, rust=1.00),
        candidate=_RunMeans(python=1.00, rust=2.00),
    )

    assert report.passed is True
    assert report.comparisons[0].regression_ratio == pytest.approx(0.0)


def test_compare_rust_regressions_fails_beyond_threshold() -> None:
    """A Rust/Python ratio increase above 10% should fail the ratchet."""
    report = _run_comparison(
        baseline=_RunMeans(python=0.25, rust=1.00),
        candidate=_RunMeans(python=0.25, rust=1.25),
    )

    assert report.passed is False
    assert report.worst_regression_ratio == pytest.approx(0.25)
    assert len(report.regressions) == 1
    assert report.regressions[0].scenario_name == "small-single-nocb"


def _multi_scenario_plan() -> dict[str, object]:
    """Return a plan payload with three comparison groups in unsorted order."""
    return {
        "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
        "dry_run": True,
        "rust_available": True,
        "worker_iterations": 20,
        "command": ["hyperfine", "placeholder"],
        "scenarios": [
            _scenario_payload(name="python-zeta", backend="python"),
            _scenario_payload(name="rust-zeta", backend="rust"),
            _scenario_payload(name="python-alpha", backend="python"),
            _scenario_payload(name="rust-alpha", backend="rust"),
            _scenario_payload(name="python-mid", backend="python"),
            _scenario_payload(name="rust-mid", backend="rust"),
        ],
    }


def _multi_scenario_throughput(
    *, zeta: float, alpha: float, mid: float
) -> dict[str, object]:
    """Return throughput results whose Rust means are the desired ratios."""
    return {
        "results": [
            {"command": "python-zeta", "mean": 1.0},
            {"command": "rust-zeta", "mean": zeta},
            {"command": "python-alpha", "mean": 1.0},
            {"command": "rust-alpha", "mean": alpha},
            {"command": "python-mid", "mean": 1.0},
            {"command": "rust-mid", "mean": mid},
        ],
    }


def test_compare_rust_regressions_sorts_scenarios_and_computes_ratios() -> None:
    """The report sorts comparisons and carries exact ratios and the threshold."""
    report = compare_rust_regressions(
        baseline=BenchmarkRunPayload(
            plan=_multi_scenario_plan(),
            throughput=_multi_scenario_throughput(zeta=2.0, alpha=1.0, mid=4.0),
            context_name="baseline",
        ),
        candidate=BenchmarkRunPayload(
            plan=_multi_scenario_plan(),
            throughput=_multi_scenario_throughput(zeta=3.0, alpha=1.5, mid=3.0),
            context_name="candidate",
        ),
        max_regression=0.10,
    )

    assert isinstance(report.comparisons, tuple), (
        "comparisons must be exposed as a tuple"
    )
    assert [entry.scenario_name for entry in report.comparisons] == [
        "alpha",
        "mid",
        "zeta",
    ], "comparisons must be ordered by sorted scenario name, not payload order"
    assert [entry.regression_ratio for entry in report.comparisons] == pytest.approx([
        0.5,
        -0.25,
        0.5,
    ]), "regression ratio must be (candidate - baseline) / baseline"
    assert all(
        entry.max_regression == pytest.approx(0.10) for entry in report.comparisons
    ), "the configured threshold must be carried onto every comparison"


def test_compare_rust_regressions_rejects_result_count_mismatch() -> None:
    """Plan/results length mismatches should fail fast."""
    candidate_throughput: dict[str, object] = {
        "results": [
            {"command": "python-only", "mean": 1.0},
        ],
    }

    with pytest.raises(ValueError, match="must match"):
        compare_rust_regressions(
            baseline=BenchmarkRunPayload(
                plan=_plan_payload(),
                throughput=_throughput_payload(python_mean=1.0, rust_mean=1.0),
                context_name="baseline",
            ),
            candidate=BenchmarkRunPayload(
                plan=_plan_payload(),
                throughput=candidate_throughput,
                context_name="candidate",
            ),
            policy=RatchetPolicy(max_regression=0.10),
        )


def test_compare_rust_regressions_rejects_missing_python_scenarios() -> None:
    """Each Rust scenario must have a matched Python scenario for its ratio."""
    rust_only_plan: dict[str, object] = {
        "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
        "dry_run": True,
        "rust_available": True,
        "worker_iterations": 20,
        "command": ["hyperfine", "placeholder"],
        "scenarios": [
            _scenario_payload(name="rust-small-single-nocb", backend="rust"),
        ],
    }
    rust_only_throughput: dict[str, object] = {
        "results": [
            {"command": "rust-small-single-nocb", "mean": 1.0},
        ],
    }

    with pytest.raises(ValueError, match="missing its Python scenario"):
        compare_rust_regressions(
            baseline=BenchmarkRunPayload(
                plan=rust_only_plan,
                throughput=rust_only_throughput,
                context_name="baseline",
            ),
            candidate=BenchmarkRunPayload(
                plan=rust_only_plan,
                throughput=rust_only_throughput,
                context_name="candidate",
            ),
            policy=RatchetPolicy(max_regression=0.10),
        )


def test_compare_rust_regressions_rejects_missing_rust_scenarios() -> None:
    """Ratchet should fail if there are no Rust scenarios to compare."""
    python_only_plan: dict[str, object] = {
        "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
        "dry_run": True,
        "rust_available": False,
        "worker_iterations": 20,
        "command": ["hyperfine", "placeholder"],
        "scenarios": [
            _scenario_payload(name="python-small-single-nocb", backend="python"),
        ],
    }
    python_only_throughput: dict[str, object] = {
        "results": [
            {"command": "python-small-single-nocb", "mean": 1.0},
        ],
    }

    with pytest.raises(ValueError, match="Rust"):
        compare_rust_regressions(
            baseline=BenchmarkRunPayload(
                plan=python_only_plan,
                throughput=python_only_throughput,
                context_name="baseline",
            ),
            candidate=BenchmarkRunPayload(
                plan=python_only_plan,
                throughput=python_only_throughput,
                context_name="candidate",
            ),
            policy=RatchetPolicy(max_regression=0.10),
        )


def test_compare_rust_regressions_rejects_invalid_backend() -> None:
    """Scenario backends must stay within the supported benchmark set."""
    invalid_plan: dict[str, object] = {
        "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
        "dry_run": True,
        "rust_available": True,
        "worker_iterations": 20,
        "command": ["hyperfine", "placeholder"],
        "scenarios": [
            _scenario_payload(name="python-small-single-nocb", backend="python"),
            _scenario_payload(name="rust-small-single-nocb", backend="native"),
        ],
    }

    with pytest.raises(ValueError, match="must be either 'python' or 'rust'"):
        compare_rust_regressions(
            baseline=BenchmarkRunPayload(
                plan=invalid_plan,
                throughput=_throughput_payload(python_mean=1.0, rust_mean=1.0),
                context_name="baseline",
            ),
            candidate=BenchmarkRunPayload(
                plan=invalid_plan,
                throughput=_throughput_payload(python_mean=1.0, rust_mean=1.0),
                context_name="candidate",
            ),
            policy=RatchetPolicy(max_regression=0.10),
        )


def test_compare_rust_regressions_rejects_duplicate_rust_scenario_names() -> None:
    """Rust scenario names must remain unique for stable matching."""
    duplicate_plan: dict[str, object] = {
        "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
        "dry_run": True,
        "rust_available": True,
        "worker_iterations": 20,
        "command": ["hyperfine", "placeholder"],
        "scenarios": [
            _scenario_payload(name="python-small-single-nocb", backend="python"),
            _scenario_payload(name="rust-small-single-nocb", backend="rust"),
            _scenario_payload(name="rust-small-single-nocb", backend="rust"),
        ],
    }
    duplicate_throughput: dict[str, object] = {
        "results": [
            {"command": "python-small-single-nocb", "mean": 1.0},
            {"command": "rust-small-single-nocb", "mean": 1.0},
            {"command": "rust-small-single-nocb", "mean": 1.1},
        ],
    }

    with pytest.raises(ValueError, match="duplicate 'rust' scenario entries"):
        compare_rust_regressions(
            baseline=BenchmarkRunPayload(
                plan=duplicate_plan,
                throughput=duplicate_throughput,
                context_name="baseline",
            ),
            candidate=BenchmarkRunPayload(
                plan=duplicate_plan,
                throughput=duplicate_throughput,
                context_name="candidate",
            ),
            policy=RatchetPolicy(max_regression=0.10),
        )


@pytest.mark.parametrize(
    ("invalid_mean", "expected_match"),
    [
        pytest.param(0.0, r"results\[1\]\.mean must be > 0", id="zero"),
        pytest.param(-1.0, r"results\[1\]\.mean must be > 0", id="negative"),
        pytest.param(float("nan"), "must be finite", id="nan"),
        pytest.param(float("inf"), "must be finite", id="pos-inf"),
        pytest.param(float("-inf"), "must be finite", id="neg-inf"),
    ],
)
def test_compare_rust_regressions_rejects_invalid_rust_mean(
    invalid_mean: float,
    expected_match: str,
) -> None:
    """Rust scenario means must be strictly positive and finite."""
    with pytest.raises(ValueError, match=expected_match):
        compare_rust_regressions(
            baseline=BenchmarkRunPayload(
                plan=_plan_payload(),
                throughput=_throughput_payload(
                    python_mean=1.0,
                    rust_mean=invalid_mean,
                ),
                context_name="baseline",
            ),
            candidate=BenchmarkRunPayload(
                plan=_plan_payload(),
                throughput=_throughput_payload(python_mean=1.0, rust_mean=1.0),
                context_name="candidate",
            ),
            policy=RatchetPolicy(max_regression=0.10),
        )


@pytest.mark.parametrize(
    ("invalid_max_regression", "expected_match"),
    [
        pytest.param(-0.01, "max_regression must be >= 0", id="negative"),
        pytest.param(float("nan"), "must be finite", id="nan"),
        pytest.param(float("inf"), "must be finite", id="pos-inf"),
        pytest.param(float("-inf"), "must be finite", id="neg-inf"),
    ],
)
def test_compare_rust_regressions_rejects_invalid_max_regression(
    invalid_max_regression: float,
    expected_match: str,
) -> None:
    """The configured slowdown threshold must be non-negative and finite."""
    with pytest.raises(ValueError, match=expected_match):
        _run_comparison(
            baseline=_RunMeans(python=1.0, rust=1.0),
            candidate=_RunMeans(python=1.0, rust=1.0),
            max_regression=invalid_max_regression,
        )
