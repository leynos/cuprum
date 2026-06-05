"""Compare baseline and candidate benchmark runs for Rust regressions.

This module loads dry-run plan JSON and hyperfine throughput JSON from two
benchmark runs (baseline and candidate), compares Rust scenario means, and
writes a structured comparison report.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import pathlib as pth
import sys  # noqa: F401
import typing as typ

from benchmarks._validation import (
    _require_list,
    _require_mapping,
    _require_non_empty_string,
)
from benchmarks.benchmark_profile import (
    IncompatibleBenchmarkProfileError,
    require_worker_iterations,
    validate_matching_profiles,
    validate_profile_version,
    write_incompatible_profile_report,
)
from benchmarks.ratchet_types import (
    BenchmarkRunPayload,
    ComparisonReport,
    ScenarioComparison,
)

_logger = logging.getLogger(__name__)


def _load_json(path: pth.Path) -> dict[str, object]:
    """Load a JSON object payload from ``path``."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        msg = f"expected a JSON object in {path}, got {type(payload).__name__}"
        raise TypeError(msg)
    return typ.cast("dict[str, object]", payload)


def _check_float_bound(
    validated: float,
    *,
    name: str,
    minimum: float,
    exclusive: bool,
) -> None:
    """Raise ``ValueError`` when *validated* does not satisfy the minimum bound."""
    minimum_text = f"{minimum:g}"
    if exclusive:
        if validated <= minimum:
            msg = f"{name} must be > {minimum_text}"
            raise ValueError(msg)
    elif validated < minimum:
        msg = f"{name} must be >= {minimum_text}"
        raise ValueError(msg)


def _require_float(
    value: object,
    *,
    name: str,
    minimum: float,
    exclusive: bool,
) -> float:
    """Validate that *value* is a finite float satisfying a minimum bound."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{name} must be a number"
        raise TypeError(msg)
    validated = float(value)
    if not math.isfinite(validated):
        msg = f"{name} must be finite"
        raise ValueError(msg)
    _check_float_bound(validated, name=name, minimum=minimum, exclusive=exclusive)
    return validated


def _require_positive_float(value: object, *, name: str) -> float:
    """Validate and return a positive float value."""
    return _require_float(value, name=name, minimum=0.0, exclusive=True)


def _require_non_negative_float(value: object, *, name: str) -> float:
    """Validate and return a non-negative float value."""
    return _require_float(value, name=name, minimum=0.0, exclusive=False)


def load_plan(path: pth.Path) -> dict[str, object]:
    """Load and minimally validate dry-run plan JSON payload."""
    _logger.debug("loading benchmark plan: path=%s", path)
    payload = _load_json(path)
    validate_profile_version(payload)
    try:
        require_worker_iterations(payload)
    except (TypeError, ValueError) as exc:
        _logger.warning("benchmark plan worker_iterations invalid: %s", exc)
        raise IncompatibleBenchmarkProfileError(str(exc)) from exc
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


def _collect_rust_means(
    scenarios: list[object],
    results: list[object],
) -> dict[str, float]:
    """Iterate paired scenarios/results and accumulate Rust means.

    Raises ``ValueError`` on duplicate Rust scenario names.
    """
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
    return rust_means


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

    rust_means = _collect_rust_means(scenarios, results)

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
    validate_matching_profiles(
        baseline_plan=baseline.plan,
        candidate_plan=candidate.plan,
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
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
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
    except IncompatibleBenchmarkProfileError as exc:
        write_incompatible_profile_report(reason=str(exc), output_path=args.output)
        _logger.info("benchmark ratchet skipped: %s", exc)
        return 0
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        _logger.error("benchmark ratchet failed to evaluate inputs: %s", exc)  # noqa: TRY400
        return 2

    if report.passed:
        _logger.info(
            "benchmark ratchet passed: %d Rust scenarios compared",
            report.rust_scenarios_compared,
        )
        return 0

    _logger.error(
        "benchmark ratchet failed: worst_regression_ratio=%.6f, max_regression=%.6f",
        report.worst_regression_ratio,
        report.max_regression,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
