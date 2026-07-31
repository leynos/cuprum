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
import sys  # noqa: F401  # re-exported for compatibility importers
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
    """Load and minimally validate dry-run plan JSON payload.

    Parameters
    ----------
    path : pth.Path
        The dry-run plan JSON file to load and validate.

    Returns
    -------
    dict[str, object]
        The validated plan payload.

    Raises
    ------
    IncompatibleBenchmarkProfileError
        If the plan's ``benchmark_profile_version`` is missing or
        incompatible, or if its ``worker_iterations`` field is invalid.
    OSError
        If ``path`` cannot be read.
    TypeError
        If the parsed JSON root is not a mapping, or if the plan's
        ``scenarios`` field or an entry within it has the wrong type.
    ValueError
        If a scenario's ``name`` or ``backend`` field is missing or empty.
    json.JSONDecodeError
        If ``path`` does not contain valid JSON.
    """  # noqa: DOC502 - propagates from _load_json and the validators
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
    """Load and minimally validate hyperfine throughput JSON payload.

    Parameters
    ----------
    path : pth.Path
        The hyperfine throughput JSON file to load and validate.

    Returns
    -------
    dict[str, object]
        The validated throughput payload.

    Raises
    ------
    OSError
        If ``path`` cannot be read.
    TypeError
        If the parsed JSON root is not a mapping, or if the payload's
        ``results`` field or an entry within it has the wrong type.
    ValueError
        If a result's ``mean`` field is missing or not a positive float.
    json.JSONDecodeError
        If ``path`` does not contain valid JSON.
    """  # noqa: DOC502 - propagates from _load_json and the validators
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
    return tuple(
        ScenarioComparison(
            scenario_name=scenario_name,
            baseline_ratio=baseline_ratios[scenario_name],
            candidate_ratio=candidate_ratios[scenario_name],
            regression_ratio=(
                (candidate_ratios[scenario_name] - baseline_ratios[scenario_name])
                / baseline_ratios[scenario_name]
            ),
            max_regression=max_regression,
        )
        for scenario_name in sorted(baseline_ratios)
    )


def compare_rust_regressions(
    *,
    baseline: BenchmarkRunPayload,
    candidate: BenchmarkRunPayload,
    max_regression: float,
) -> ComparisonReport:
    """Compare within-run Rust/Python ratios and evaluate the threshold.

    Parameters
    ----------
    baseline : BenchmarkRunPayload
        The baseline benchmark run payload defining the reference ratios.
    candidate : BenchmarkRunPayload
        The candidate benchmark run payload compared against the baseline.
    max_regression : float
        The maximum tolerated Rust/Python ratio regression; must be
        non-negative.

    Returns
    -------
    ComparisonReport
        The per-scenario comparison and overall pass/fail verdict.

    Raises
    ------
    TypeError
        If ``max_regression`` is not a number, as rejected by
        ``_require_non_negative_float``, or if a malformed ``scenarios`` or
        ``results`` payload propagates from ``_extract_rust_python_ratios``.
    ValueError
        If validation fails in ``_extract_rust_python_ratios``,
        ``_validate_matching_comparison_groups``, or
        ``_require_non_negative_float``.
    IncompatibleBenchmarkProfileError
        If ``validate_matching_profiles`` finds incompatible profile
        metadata between the baseline and candidate plans.
    """  # noqa: DOC502 - all raised exceptions propagate from validator calls
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
    """Execute benchmark ratchet comparison and return process exit code.

    Returns
    -------
    int
        The process exit code: ``0`` on pass or skip, ``1`` on regression,
        ``2`` on invalid inputs.

    Raises
    ------
    SystemExit
        If ``_parse_args`` rejects invalid or missing command-line
        arguments.
    """  # noqa: DOC502 - SystemExit propagates from _parse_args via argparse
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
        _logger.error(  # noqa: TRY400  # invalid inputs are an expected CLI outcome
            "benchmark ratchet failed to evaluate inputs: %s",
            exc,
        )
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
