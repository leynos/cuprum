"""Compare baseline and candidate benchmark runs for Rust regressions.

This module loads dry-run plan JSON and hyperfine throughput JSON from two
benchmark runs (baseline and candidate), compares Rust scenario means, and
writes a structured comparison report.
"""

from __future__ import annotations

import argparse
import dataclasses as dc
import json
import pathlib as pth
import sys
import typing as typ

_FLOAT_TOLERANCE = 1e-12


@dc.dataclass(frozen=True, slots=True)
class ScenarioComparison:
    """Comparison result for one Rust benchmark scenario."""

    scenario_name: str
    baseline_mean: float
    candidate_mean: float
    regression_ratio: float
    max_regression: float

    @property
    def is_regression(self) -> bool:
        """Return ``True`` when scenario regression exceeds threshold."""
        return (self.regression_ratio - self.max_regression) > _FLOAT_TOLERANCE

    def as_dict(self) -> dict[str, object]:
        """Serialize the scenario comparison for JSON output."""
        return {
            "scenario_name": self.scenario_name,
            "baseline_mean": self.baseline_mean,
            "candidate_mean": self.candidate_mean,
            "regression_ratio": self.regression_ratio,
            "max_regression": self.max_regression,
            "is_regression": self.is_regression,
        }


@dc.dataclass(frozen=True, slots=True)
class ComparisonReport:
    """Summary report for Rust benchmark regression comparison."""

    max_regression: float
    comparisons: tuple[ScenarioComparison, ...]

    @property
    def passed(self) -> bool:
        """Return ``True`` when no scenario breaches the configured threshold."""
        return all(not comparison.is_regression for comparison in self.comparisons)

    @property
    def regressions(self) -> tuple[ScenarioComparison, ...]:
        """Return comparisons that breached the regression threshold."""
        return tuple(
            comparison for comparison in self.comparisons if comparison.is_regression
        )

    @property
    def rust_scenarios_compared(self) -> int:
        """Return the number of Rust scenarios included in the comparison."""
        return len(self.comparisons)

    @property
    def worst_regression_ratio(self) -> float:
        """Return the worst regression ratio across all compared scenarios."""
        if not self.comparisons:
            return 0.0
        return max(comparison.regression_ratio for comparison in self.comparisons)

    def as_dict(self) -> dict[str, object]:
        """Serialize the report for JSON output."""
        return {
            "max_regression": self.max_regression,
            "passed": self.passed,
            "rust_scenarios_compared": self.rust_scenarios_compared,
            "worst_regression_ratio": self.worst_regression_ratio,
            "comparisons": [comparison.as_dict() for comparison in self.comparisons],
            "regressions": [comparison.as_dict() for comparison in self.regressions],
        }


@dc.dataclass(frozen=True, slots=True)
class BenchmarkRunPayload:
    """Benchmark payload pair for one context (baseline or candidate)."""

    plan: dict[str, object]
    throughput: dict[str, object]
    context_name: str


def _load_json(path: pth.Path) -> dict[str, object]:
    """Load a JSON object payload from ``path``."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        msg = f"expected a JSON object in {path}, got {type(payload).__name__}"
        raise TypeError(msg)
    return typ.cast("dict[str, object]", payload)


def _require_list(value: object, *, name: str) -> list[object]:
    """Validate and return a list value."""
    if not isinstance(value, list):
        msg = f"{name} must be a list"
        raise TypeError(msg)
    return typ.cast("list[object]", value)


def _require_mapping(value: object, *, name: str) -> dict[str, object]:
    """Validate and return a mapping-like JSON object."""
    if not isinstance(value, dict):
        msg = f"{name} must be an object"
        raise TypeError(msg)
    return typ.cast("dict[str, object]", value)


def _require_non_empty_string(value: object, *, name: str) -> str:
    """Validate and return a non-empty string."""
    if not isinstance(value, str) or not value.strip():
        msg = f"{name} must be a non-empty string"
        raise ValueError(msg)
    return value


def _require_positive_float(value: object, *, name: str) -> float:
    """Validate and return a positive float value."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{name} must be a number"
        raise TypeError(msg)
    validated = float(value)
    if validated <= 0.0:
        msg = f"{name} must be > 0"
        raise ValueError(msg)
    return validated


def _require_non_negative_float(value: object, *, name: str) -> float:
    """Validate and return a non-negative float value."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{name} must be a number"
        raise TypeError(msg)
    validated = float(value)
    if validated < 0.0:
        msg = f"{name} must be >= 0"
        raise ValueError(msg)
    return validated


def load_plan(path: pth.Path) -> dict[str, object]:
    """Load and minimally validate dry-run plan JSON payload."""
    payload = _load_json(path)
    scenarios = _require_list(payload.get("scenarios"), name="scenarios")

    for index, scenario_value in enumerate(scenarios):
        scenario = _require_mapping(
            scenario_value,
            name=f"scenarios[{index}]",
        )
        _require_non_empty_string(
            scenario.get("name"),
            name=f"scenarios[{index}].name",
        )
        _require_non_empty_string(
            scenario.get("backend"),
            name=f"scenarios[{index}].backend",
        )

    return payload


def load_throughput(path: pth.Path) -> dict[str, object]:
    """Load and minimally validate hyperfine throughput JSON payload."""
    payload = _load_json(path)
    results = _require_list(payload.get("results"), name="results")

    for index, result_value in enumerate(results):
        result = _require_mapping(result_value, name=f"results[{index}]")
        _require_positive_float(result.get("mean"), name=f"results[{index}].mean")

    return payload


def _validate_backend(backend: str, *, index: int) -> None:
    """Raise ``ValueError`` when *backend* is not 'python' or 'rust'."""
    if backend not in {"python", "rust"}:
        msg = (
            f"scenarios[{index}].backend must be either 'python' or 'rust', "
            f"got {backend!r}"
        )
        raise ValueError(msg)


def _extract_rust_entry(
    *,
    index: int,
    scenario_value: object,
    result_value: object,
) -> tuple[str, float] | None:
    """Return ``(scenario_name, mean)`` for a Rust entry, or ``None`` for non-Rust."""
    scenario = _require_mapping(scenario_value, name=f"scenarios[{index}]")
    result = _require_mapping(result_value, name=f"results[{index}]")

    backend = _require_non_empty_string(
        scenario.get("backend"), name=f"scenarios[{index}].backend"
    )
    scenario_name = _require_non_empty_string(
        scenario.get("name"), name=f"scenarios[{index}].name"
    )
    _validate_backend(backend, index=index)

    if backend != "rust":
        return None

    mean = _require_positive_float(result.get("mean"), name=f"results[{index}].mean")
    return scenario_name, mean


def _extract_rust_means(
    *,
    plan_payload: dict[str, object],
    throughput_payload: dict[str, object],
    context_name: str,
) -> dict[str, float]:
    """Map Rust scenario names to mean runtimes for one benchmark run."""
    scenarios = _require_list(plan_payload.get("scenarios"), name="scenarios")
    results = _require_list(throughput_payload.get("results"), name="results")

    if len(scenarios) != len(results):
        msg = (
            f"{context_name}: plan scenario count ({len(scenarios)}) must match "
            f"throughput result count ({len(results)})"
        )
        raise ValueError(msg)

    rust_means: dict[str, float] = {}
    for index, (scenario_value, result_value) in enumerate(
        zip(scenarios, results, strict=True)
    ):
        entry = _extract_rust_entry(
            index=index,
            scenario_value=scenario_value,
            result_value=result_value,
        )
        if entry is None:
            continue
        scenario_name, mean = entry
        if scenario_name in rust_means:
            msg = f"duplicate Rust scenario name: {scenario_name!r}"
            raise ValueError(msg)
        rust_means[scenario_name] = mean

    if not rust_means:
        msg = f"{context_name}: Rust scenarios are required for ratchet comparison"
        raise ValueError(msg)

    return rust_means


def _validate_matching_rust_scenarios(
    *,
    baseline_rust_means: dict[str, float],
    candidate_rust_means: dict[str, float],
) -> None:
    """Validate that baseline and candidate have the same Rust scenarios."""
    baseline_names = set(baseline_rust_means)
    candidate_names = set(candidate_rust_means)
    if baseline_names != candidate_names:
        missing_from_candidate = sorted(baseline_names - candidate_names)
        missing_from_baseline = sorted(candidate_names - baseline_names)
        msg = (
            "Rust scenario sets must match across baseline and candidate runs: "
            f"missing_from_candidate={missing_from_candidate}, "
            f"missing_from_baseline={missing_from_baseline}"
        )
        raise ValueError(msg)


def compare_rust_regressions(
    *,
    baseline: BenchmarkRunPayload,
    candidate: BenchmarkRunPayload,
    max_regression: float,
) -> ComparisonReport:
    """Compare Rust scenario means and evaluate the regression threshold."""
    validated_max_regression = _require_non_negative_float(
        max_regression,
        name="max_regression",
    )

    baseline_rust_means = _extract_rust_means(
        plan_payload=baseline.plan,
        throughput_payload=baseline.throughput,
        context_name=baseline.context_name,
    )
    candidate_rust_means = _extract_rust_means(
        plan_payload=candidate.plan,
        throughput_payload=candidate.throughput,
        context_name=candidate.context_name,
    )
    _validate_matching_rust_scenarios(
        baseline_rust_means=baseline_rust_means,
        candidate_rust_means=candidate_rust_means,
    )

    comparisons: list[ScenarioComparison] = []
    for scenario_name in sorted(baseline_rust_means):
        baseline_mean = baseline_rust_means[scenario_name]
        candidate_mean = candidate_rust_means[scenario_name]
        regression_ratio = (candidate_mean - baseline_mean) / baseline_mean
        comparisons.append(
            ScenarioComparison(
                scenario_name=scenario_name,
                baseline_mean=baseline_mean,
                candidate_mean=candidate_mean,
                regression_ratio=regression_ratio,
                max_regression=validated_max_regression,
            ),
        )

    return ComparisonReport(
        max_regression=validated_max_regression,
        comparisons=tuple(comparisons),
    )


def write_report(*, report: ComparisonReport, output_path: pth.Path) -> None:
    """Write comparison report JSON to ``output_path``."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report.as_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the benchmark ratchet CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-plan", type=pth.Path, required=True)
    parser.add_argument("--baseline-throughput", type=pth.Path, required=True)
    parser.add_argument("--candidate-plan", type=pth.Path, required=True)
    parser.add_argument("--candidate-throughput", type=pth.Path, required=True)
    parser.add_argument(
        "--max-regression",
        type=float,
        default=0.10,
        help="Maximum allowed slowdown ratio for Rust scenarios.",
    )
    parser.add_argument("--output", type=pth.Path, required=True)
    return parser.parse_args()


def main() -> int:
    """Execute benchmark ratchet comparison and return process exit code."""
    args = _parse_args()
    try:
        report = compare_rust_regressions(
            baseline=BenchmarkRunPayload(
                plan=load_plan(args.baseline_plan),
                throughput=load_throughput(args.baseline_throughput),
                context_name="baseline",
            ),
            candidate=BenchmarkRunPayload(
                plan=load_plan(args.candidate_plan),
                throughput=load_throughput(args.candidate_throughput),
                context_name="candidate",
            ),
            max_regression=args.max_regression,
        )
        write_report(report=report, output_path=args.output)
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        print(f"benchmark ratchet failed to evaluate inputs: {exc}", file=sys.stderr)
        return 2

    if report.passed:
        print(
            "benchmark ratchet passed: "
            f"{report.rust_scenarios_compared} Rust scenarios compared",
        )
        return 0

    print(
        "benchmark ratchet failed: "
        f"worst_regression_ratio={report.worst_regression_ratio:.6f}, "
        f"max_regression={report.max_regression:.6f}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
