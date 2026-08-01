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
from benchmarks.ratchet_ratio_extraction import (
    _comparison_id_for_scenario,  # noqa: F401  # re-exported for importers
    _extract_rust_python_ratios,
    _validate_backend,  # noqa: F401  # re-exported for importers
    _validate_matching_comparison_groups,
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


def _build_scenario_comparisons(
    *,
    baseline_ratios: dict[str, float],
    candidate_ratios: dict[str, float],
    max_regression: float,
) -> tuple[ScenarioComparison, ...]:
    """Build ordered scenario comparisons from validated ratio maps."""
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
                max_regression=max_regression,
            ),
        )
    return tuple(comparisons)


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

    comparisons = _build_scenario_comparisons(
        baseline_ratios=baseline_ratios,
        candidate_ratios=candidate_ratios,
        max_regression=validated_max_regression,
    )

    return ComparisonReport(
        max_regression=validated_max_regression,
        comparisons=comparisons,
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
