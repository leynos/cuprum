"""Compare baseline and candidate benchmark runs for Rust regressions.

This module loads dry-run plan JSON and hyperfine throughput JSON from two
benchmark runs (baseline and candidate), compares the Rust-to-Python mean
ratio for each matched scenario pair, and writes a structured comparison
report. Ratcheting on the within-run ratio rather than absolute wall-clock
means cancels out runner-speed differences and interpreter startup overhead
between the two CI jobs that produced the runs.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib as pth
import sys  # noqa: F401
import typing as typ

from benchmarks._validation import (
    _require_list,
    _require_mapping,
    _require_non_empty_string,
    _require_non_negative_float,
    _require_positive_float,
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


def _comparison_id_for_scenario(*, scenario_name: str, backend: str) -> str:
    """Return the backend-independent comparison identifier for one scenario."""
    prefix = f"{backend}-"
    if not scenario_name.startswith(prefix):
        msg = (
            f"scenario name {scenario_name!r} must start with expected backend "
            f"prefix {prefix!r}"
        )
        raise ValueError(msg)
    comparison_id = scenario_name.removeprefix(prefix)
    if not comparison_id:
        msg = f"scenario name {scenario_name!r} must include a comparison label"
        raise ValueError(msg)
    return comparison_id


def _extract_scenario_entry(
    *,
    index: int,
    scenario_value: object,
    result_value: object,
) -> tuple[str, str, float]:
    """Return ``(comparison_id, backend, mean)`` for one paired entry."""
    scenario = _require_mapping(scenario_value, name=f"scenarios[{index}]")
    result = _require_mapping(result_value, name=f"results[{index}]")

    backend = _require_non_empty_string(
        scenario.get("backend"), name=f"scenarios[{index}].backend"
    )
    scenario_name = _require_non_empty_string(
        scenario.get("name"), name=f"scenarios[{index}].name"
    )
    _validate_backend(backend, index=index)
    comparison_id = _comparison_id_for_scenario(
        scenario_name=scenario_name,
        backend=backend,
    )

    mean = _require_positive_float(result.get("mean"), name=f"results[{index}].mean")
    return comparison_id, backend, mean


def _collect_backend_means(
    scenarios: list[object],
    results: list[object],
) -> dict[str, dict[str, float]]:
    """Group mean runtimes by comparison identifier and backend.

    Raises ``ValueError`` when a comparison group contains duplicate entries
    for the same backend.
    """
    grouped: dict[str, dict[str, float]] = {}
    for index, (scenario_value, result_value) in enumerate(
        zip(scenarios, results, strict=True)
    ):
        comparison_id, backend, mean = _extract_scenario_entry(
            index=index,
            scenario_value=scenario_value,
            result_value=result_value,
        )
        group = grouped.setdefault(comparison_id, {})
        if backend in group:
            msg = (
                f"comparison group {comparison_id!r} contains duplicate "
                f"{backend!r} scenario entries"
            )
            raise ValueError(msg)
        group[backend] = mean
    return grouped


def _extract_rust_python_ratios(
    *,
    plan_payload: dict[str, object],
    throughput_payload: dict[str, object],
    context_name: str,
) -> dict[str, float]:
    """Map comparison identifiers to within-run Rust/Python mean ratios.

    Each ratio divides the Rust scenario mean by the matched Python scenario
    mean from the same benchmark run, so runner speed and interpreter startup
    overhead cancel out of the cross-run comparison.
    """
    scenarios = _require_list(plan_payload.get("scenarios"), name="scenarios")
    results = _require_list(throughput_payload.get("results"), name="results")

    if len(scenarios) != len(results):
        msg = (
            f"{context_name}: plan scenario count ({len(scenarios)}) must match "
            f"throughput result count ({len(results)})"
        )
        raise ValueError(msg)

    grouped = _collect_backend_means(scenarios, results)

    ratios: dict[str, float] = {}
    for comparison_id in sorted(grouped):
        group = grouped[comparison_id]
        python_mean = group.get("python")
        rust_mean = group.get("rust")
        if python_mean is None:
            msg = (
                f"{context_name}: comparison group {comparison_id!r} is missing "
                "its Python scenario"
            )
            raise ValueError(msg)
        if rust_mean is None:
            msg = (
                f"{context_name}: comparison group {comparison_id!r} is missing "
                "its Rust scenario"
            )
            raise ValueError(msg)
        ratios[comparison_id] = rust_mean / python_mean

    if not ratios:
        msg = f"{context_name}: Rust scenarios are required for ratchet comparison"
        raise ValueError(msg)

    return ratios


def _validate_matching_comparison_groups(
    *,
    baseline_ratios: dict[str, float],
    candidate_ratios: dict[str, float],
) -> None:
    """Validate that baseline and candidate have the same comparison groups."""
    baseline_names = set(baseline_ratios)
    candidate_names = set(candidate_ratios)
    if baseline_names != candidate_names:
        missing_from_candidate = sorted(baseline_names - candidate_names)
        missing_from_baseline = sorted(candidate_names - baseline_names)
        msg = (
            "comparison groups must match across baseline and candidate runs: "
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
    """Compare within-run Rust/Python ratios and evaluate the threshold."""
    validated_max_regression = _require_non_negative_float(
        max_regression,
        name="max_regression",
    )
    validate_matching_profiles(
        baseline_plan=baseline.plan,
        candidate_plan=candidate.plan,
    )

    baseline_ratios = _extract_rust_python_ratios(
        plan_payload=baseline.plan,
        throughput_payload=baseline.throughput,
        context_name=baseline.context_name,
    )
    candidate_ratios = _extract_rust_python_ratios(
        plan_payload=candidate.plan,
        throughput_payload=candidate.throughput,
        context_name=candidate.context_name,
    )
    _validate_matching_comparison_groups(
        baseline_ratios=baseline_ratios,
        candidate_ratios=candidate_ratios,
    )

    comparisons: list[ScenarioComparison] = []
    for scenario_name in sorted(baseline_ratios):
        baseline_ratio = baseline_ratios[scenario_name]
        candidate_ratio = candidate_ratios[scenario_name]
        regression_ratio = (candidate_ratio - baseline_ratio) / baseline_ratio
        comparisons.append(
            ScenarioComparison(
                scenario_name=scenario_name,
                baseline_ratio=baseline_ratio,
                candidate_ratio=candidate_ratio,
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
        default=0.30,
        help=(
            "Maximum allowed relative increase in the within-run Rust/Python "
            "mean ratio for any scenario pair."
        ),
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
