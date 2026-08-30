"""Compare baseline and candidate benchmark runs for Rust regressions.

This module loads dry-run plan JSON and hyperfine throughput JSON from a
candidate benchmark run, compares the Rust-to-Python mean ratio for each
matched scenario pair against the main-branch bar, and writes a structured
comparison report. Ratcheting on the within-run ratio rather than absolute
wall-clock means cancels out runner-speed differences and interpreter
startup overhead between the CI jobs that produced the runs.

The bar is the median of a rolling window of main-branch samples, and a
scenario fails only when it exceeds both the configured flat threshold and
the spread those samples exhibited — see `benchmarks/ratchet_history.py` for
why. A run with no window falls back to a single-sample baseline, which is
the bar this ratchet used before the window existed.

Reading runs into ratios lives in `benchmarks/ratchet_ratios.py`; this
module decides what those ratios mean. `load_plan`, `load_throughput`,
`run_ratios` and `profile_metadata` are re-exported because callers have
always reached them here.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib as pth
import sys  # noqa: F401
import typing as typ

from benchmarks.benchmark_profile import (
    IncompatibleBenchmarkProfileError,
    validate_matching_profiles,
    write_incompatible_profile_report,
)
from benchmarks.ratchet_history import (
    DEFAULT_NOISE_SIGMAS,
    DEFAULT_WINDOW_SIZE,
    BaselineHistory,
    RatchetPolicy,
    load_history,
    median_ratio,
    noise_tolerance,
)
from benchmarks.ratchet_ratios import (
    load_plan,
    load_throughput,
    profile_metadata,
    run_ratios,
)
from benchmarks.ratchet_types import (
    BenchmarkRunPayload,
    ComparisonReport,
    ScenarioComparison,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = [
    "compare_rust_regressions",
    "load_plan",
    "load_throughput",
    "main",
    "profile_metadata",
    "run_ratios",
    "write_report",
]

_logger = logging.getLogger(__name__)


def _validate_matching_comparison_groups(
    *,
    baseline_ratios: cabc.Mapping[str, object],
    candidate_ratios: cabc.Mapping[str, object],
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


def _baseline_window(
    *,
    baseline: BenchmarkRunPayload | None,
    candidate: BenchmarkRunPayload,
    history: BaselineHistory | None,
    window_size: int,
) -> dict[str, tuple[float, ...]]:
    """Return the per-scenario main-branch samples the candidate is judged against.

    The window is preferred whenever it holds a compatible sample. The
    single-sample baseline is the fallback for a first run, an expired
    artefact, or a window emptied by a benchmark-profile change — a worse bar
    than the window, but the one this ratchet used before the window existed.
    """
    recent = _compatible_history_window(
        candidate=candidate,
        history=history,
        window_size=window_size,
    )
    if recent.samples:
        return {name: recent.ratios_for(name) for name in recent.scenarios}

    if baseline is None:
        msg = "a baseline run or a non-empty baseline history is required"
        raise ValueError(msg)
    _logger.info("no compatible baseline history; comparing against one sample")
    validate_matching_profiles(
        baseline_plan=baseline.plan,
        candidate_plan=candidate.plan,
    )
    return {name: (ratio,) for name, ratio in run_ratios(baseline).items()}

def _compatible_history_window(
    *,
    candidate: BenchmarkRunPayload,
    history: BaselineHistory | None,
    window_size: int,
) -> BaselineHistory:
    """Return recent history samples compatible with the candidate's profile."""
    version, worker_iterations = profile_metadata(candidate.plan)
    compatible = (
        history.compatible_with(
            benchmark_profile_version=version,
            worker_iterations=worker_iterations,
        )
        if history is not None
        else BaselineHistory()
    )
    return BaselineHistory(samples=compatible.samples[-window_size:])
def _compare_scenario(
    *,
    scenario_name: str,
    samples: tuple[float, ...],
    candidate_ratio: float,
    policy: RatchetPolicy,
) -> ScenarioComparison:
    """Judge one scenario against its window of main-branch samples."""
    baseline_ratio = median_ratio(samples)
    return ScenarioComparison(
        scenario_name=scenario_name,
        baseline_ratio=baseline_ratio,
        candidate_ratio=candidate_ratio,
        regression_ratio=(candidate_ratio - baseline_ratio) / baseline_ratio,
        max_regression=policy.max_regression,
        baseline_sample_count=len(samples),
        noise_tolerance=noise_tolerance(samples, sigmas=policy.noise_sigmas),
    )


def compare_rust_regressions(
    *,
    candidate: BenchmarkRunPayload,
    baseline: BenchmarkRunPayload | None = None,
    history: BaselineHistory | None = None,
    policy: RatchetPolicy | None = None,
    max_regression: float | None = None,
) -> ComparisonReport:
    """Compare within-run Rust/Python ratios and evaluate the threshold."""
    if policy is not None and max_regression is not None:
        msg = "pass either policy or max_regression, not both"
        raise ValueError(msg)
    resolved = (
        policy
        if policy is not None
        else RatchetPolicy()
        if max_regression is None
        else RatchetPolicy(max_regression=max_regression)
    )
    window = _baseline_window(
        baseline=baseline,
        candidate=candidate,
        history=history,
        window_size=resolved.window_size,
    )
    candidate_ratios = run_ratios(candidate)
    _validate_matching_comparison_groups(
        baseline_ratios=window,
        candidate_ratios=candidate_ratios,
    )

    return ComparisonReport(
        max_regression=resolved.max_regression,
        comparisons=tuple(
            _compare_scenario(
                scenario_name=scenario_name,
                samples=window[scenario_name],
                candidate_ratio=candidate_ratios[scenario_name],
                policy=resolved,
            )
            for scenario_name in sorted(window)
        ),
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
    parser.add_argument(
        "--baseline-plan",
        type=pth.Path,
        help=(
            "Single-sample fallback baseline plan, used when no compatible "
            "baseline history is available."
        ),
    )
    parser.add_argument("--baseline-throughput", type=pth.Path)
    parser.add_argument(
        "--baseline-history",
        type=pth.Path,
        help=(
            "Rolling window of recent main-branch samples. An absent or "
            "unreadable file falls back to the single-sample baseline."
        ),
    )
    parser.add_argument("--candidate-plan", type=pth.Path, required=True)
    parser.add_argument("--candidate-throughput", type=pth.Path, required=True)
    parser.add_argument(
        "--max-regression",
        type=float,
        default=0.30,
        help=(
            "Minimum relative increase in the within-run Rust/Python mean "
            "ratio that can count as a regression, whatever the observed "
            "spread. The wider of this and the noise band decides."
        ),
    )
    parser.add_argument(
        "--noise-sigmas",
        type=float,
        default=DEFAULT_NOISE_SIGMAS,
        help=(
            "Estimated standard deviations of the window's observed spread a "
            "candidate must exceed before its regression counts. Zero judges "
            "on --max-regression alone."
        ),
    )
    parser.add_argument(
        "--history-window",
        type=int,
        default=DEFAULT_WINDOW_SIZE,
        help="How many of the most recent history samples to compare against.",
    )
    parser.add_argument("--output", type=pth.Path, required=True)
    return parser.parse_args()


def _load_baseline(args: argparse.Namespace) -> BenchmarkRunPayload | None:
    """Load the single-sample fallback baseline, when one was supplied."""
    if args.baseline_plan is None or args.baseline_throughput is None:
        return None
    return BenchmarkRunPayload(
        plan=load_plan(args.baseline_plan),
        throughput=load_throughput(args.baseline_throughput),
        context_name="baseline",
    )


def main() -> int:
    """Execute benchmark ratchet comparison and return process exit code."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args()
    try:
        candidate = BenchmarkRunPayload(
            plan=load_plan(args.candidate_plan),
            throughput=load_throughput(args.candidate_throughput),
            context_name="candidate",
        )
        history = _compatible_history_window(
            candidate=candidate,
            history=load_history(args.baseline_history),
            window_size=args.history_window,
        )
        report = compare_rust_regressions(
            baseline=None if history.samples else _load_baseline(args),
            candidate=candidate,
            history=history,
            policy=RatchetPolicy(
                max_regression=args.max_regression,
                noise_sigmas=args.noise_sigmas,
                window_size=args.history_window,
            ),
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
            "benchmark ratchet passed: %d Rust scenarios compared against %d "
            "main-branch sample(s)",
            report.rust_scenarios_compared,
            report.baseline_sample_count,
        )
        return 0

    _logger.error(
        "benchmark ratchet failed: worst_regression_ratio=%.6f, "
        "max_regression=%.6f, baseline_samples=%d",
        report.worst_regression_ratio,
        report.max_regression,
        report.baseline_sample_count,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
