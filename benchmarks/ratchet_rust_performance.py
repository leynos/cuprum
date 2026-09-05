"""Orchestrate the Rust/Python benchmark ratchet against `main` baselines.

`benchmarks.ratchet_ratios` loads and validates plan and throughput payloads,
then invokes the canonical ratio extractor to derive within-run Rust/Python
ratios.

`compare_rust_regressions` selects history or a fallback. `RatchetPolicy`,
median baselines, and MAD tolerance determine each regression threshold.

The orchestration produces `ComparisonReport` and `ScenarioComparison` values.
`write_report` and `main` adapt them to JSON and process status: `0` passes or
intentionally does not compare, `1` reports a regression, and `2` reports
invalid input or processing failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib as pth
import typing as typ

from benchmarks.benchmark_profile import (
    IncompatibleBenchmarkProfileError,
    write_incompatible_profile_report,
)
from benchmarks.ratchet_baseline import _baseline_window, _compatible_history_window
from benchmarks.ratchet_history import (
    DEFAULT_NOISE_SIGMAS,
    DEFAULT_WINDOW_SIZE,
    BaselineHistory,
    BaselineHistoryNotFoundError,
    RatchetPolicy,
    load_history,
    median_ratio,
    noise_tolerance,
)
from benchmarks.ratchet_ratio_extraction import _validate_matching_comparison_groups
from benchmarks.ratchet_ratios import (
    load_plan,
    load_throughput,
    run_ratios,
)
from benchmarks.ratchet_ratios import profile_metadata as profile_metadata
from benchmarks.ratchet_types import (
    BaselineReason,
    BaselineSource,
    BenchmarkRunPayload,
    ComparisonReport,
    ComparisonState,
    RatchetDecision,
    ScenarioComparison,
)

__all__ = [
    "compare_rust_regressions",
    *("load_plan", "load_throughput", "main"),
    *("profile_metadata", "run_ratios", "write_report"),
]
_logger = logging.getLogger(__name__)
if typ.TYPE_CHECKING:
    import collections.abc as cabc


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


def _policy_options(options: dict[str, object]) -> tuple[object | None, object | None]:
    """Consume and return the supported comparison-policy options."""
    policy = options.pop("policy", None)
    max_regression = options.pop("max_regression", None)
    if options:
        msg = f"unsupported comparison option(s): {', '.join(sorted(options))}"
        raise TypeError(msg)
    return policy, max_regression


def _validated_policy_option(policy: object | None) -> RatchetPolicy | None:
    """Return a `RatchetPolicy` option after validating its type."""
    if policy is not None and not isinstance(policy, RatchetPolicy):
        msg = "policy must be a RatchetPolicy"
        raise TypeError(msg)
    return policy


def _validated_max_regression_option(max_regression: object | None) -> float | None:
    """Return a floating-point legacy threshold after validating its type."""
    if max_regression is not None and not isinstance(max_regression, float):
        msg = "max_regression must be a float"
        raise TypeError(msg)
    return max_regression


def _validate_exclusive_policy_options(
    *, policy: RatchetPolicy | None, max_regression: float | None
) -> None:
    """Reject supplying both policy representations at once."""
    if policy is not None and max_regression is not None:
        msg = "pass either policy or max_regression, not both"
        raise ValueError(msg)


def _validate_policy_options(
    *, policy: object | None, max_regression: object | None
) -> tuple[RatchetPolicy | None, float | None]:
    """Type-check policy options before enforcing their mutual exclusion."""
    valid_policy = _validated_policy_option(policy)
    valid_max = _validated_max_regression_option(max_regression)
    _validate_exclusive_policy_options(policy=valid_policy, max_regression=valid_max)
    return valid_policy, valid_max


def _comparison_policy(options: dict[str, object]) -> RatchetPolicy:
    """Resolve supported policy keywords, including the legacy threshold."""
    policy, max_regression = _policy_options(options)
    policy, max_regression = _validate_policy_options(
        policy=policy,
        max_regression=max_regression,
    )
    if policy is not None:
        return policy
    if max_regression is not None:
        return RatchetPolicy(max_regression=max_regression)
    return RatchetPolicy()


def compare_rust_regressions(
    *,
    candidate: BenchmarkRunPayload,
    baseline: BenchmarkRunPayload | None = None,
    history: BaselineHistory | None = None,
    **options: object,
) -> ComparisonReport:
    """Compare within-run Rust/Python ratios against a baseline window.

    Parameters
    ----------
    candidate : BenchmarkRunPayload
        Pull-request measurement to judge.
    baseline : BenchmarkRunPayload | None
        Single-sample fallback when ``history`` is empty.
    history : BaselineHistory | None
        Compatible main-branch history used as the preferred baseline.
    **options : object
        Optional ``RatchetPolicy`` or legacy ``max_regression`` setting.

    Returns
    -------
    ComparisonReport
        Per-scenario verdicts and their effective thresholds.

    Raises
    ------
    TypeError, ValueError
        If the policy, baseline, or comparison groups are invalid.
    """  # ruff: ignore[docstring-extraneous-exception] - policy and payload validation intentionally propagate their contract errors.
    resolved = _comparison_policy(options)
    window, decision = _baseline_window(
        baseline=baseline,
        candidate=candidate,
        history=history,
        window_size=resolved.window_size,
    )
    candidate_ratios = run_ratios(candidate)
    _validate_matching_comparison_groups(
        baseline_ratios=window, candidate_ratios=candidate_ratios
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
        decision=decision,
    )


def write_report(*, report: ComparisonReport, output_path: pth.Path) -> None:
    """Write a comparison report as stable, formatted JSON.

    Parameters
    ----------
    report : ComparisonReport
        Evaluated ratchet result to serialize.
    output_path : pathlib.Path
        JSON destination; missing parent directories are created.

    Raises
    ------
    OSError, TypeError
        If the destination cannot be prepared or written, or serialization
        fails. The CLI reports the failure without publishing a partial
        verdict.
    """  # ruff: ignore[docstring-extraneous-exception] - file and JSON operations deliberately propagate their errors.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report.as_dict(), indent=2, sort_keys=True)
    output_path.write_text(payload, encoding="utf-8")


def _parse_args(argv: cabc.Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the benchmark ratchet CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline-plan",
        type=pth.Path,
        help="Single-sample fallback plan when no compatible history is available.",
    )
    parser.add_argument(
        "--baseline-throughput",
        type=pth.Path,
        help="Fallback throughput when compatible history is unavailable.",
    )
    parser.add_argument(
        "--baseline-history",
        type=pth.Path,
        help="History window; an absent path uses the single-sample fallback.",
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
    return parser.parse_args(argv)


def _load_baseline(args: argparse.Namespace) -> BenchmarkRunPayload | None:
    """Load the single-sample fallback baseline, when one was supplied."""
    if args.baseline_plan is None and args.baseline_throughput is None:
        return None
    if args.baseline_plan is None or args.baseline_throughput is None:
        msg = "--baseline-plan and --baseline-throughput must be supplied together"
        raise ValueError(msg)
    return BenchmarkRunPayload(
        plan=load_plan(args.baseline_plan),
        throughput=load_throughput(args.baseline_throughput),
        context_name="baseline",
    )


def _load_optional_history(path: pth.Path | None) -> BaselineHistory | None:
    """Load optional history while preserving whether it was unavailable."""
    if path is not None:
        try:
            return load_history(path)
        except BaselineHistoryNotFoundError:
            pass
    _logger.info("no baseline history is available; using the fallback baseline")
    return None


def _evaluate_ratchet(args: argparse.Namespace) -> ComparisonReport:
    """Evaluate CLI inputs and return the resulting ratchet report."""
    candidate = BenchmarkRunPayload(
        plan=load_plan(args.candidate_plan),
        throughput=load_throughput(args.candidate_throughput),
        context_name="candidate",
    )
    history = _load_optional_history(args.baseline_history)
    compatible_history = _compatible_history_window(
        candidate=candidate,
        history=history,
        window_size=args.history_window,
    )
    baseline = None if compatible_history.samples else _load_baseline(args)
    if baseline is None and not compatible_history.samples:
        return ComparisonReport(
            max_regression=args.max_regression,
            comparisons=(),
            decision=RatchetDecision(
                baseline_source=BaselineSource.NONE,
                baseline_reason=(
                    BaselineReason.NO_BASELINE_AVAILABLE
                    if history is None
                    else BaselineReason.NO_COMPATIBLE_HISTORY
                ),
                compatible_sample_count=0,
                comparison_state=ComparisonState.SKIPPED_NO_BASELINE,
            ),
            baseline_available=False,
            comparison_performed=False,
        )
    return compare_rust_regressions(
        baseline=baseline,
        candidate=candidate,
        history=history,
        policy=RatchetPolicy(
            max_regression=args.max_regression,
            noise_sigmas=args.noise_sigmas,
            window_size=args.history_window,
        ),
    )


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Execute the benchmark ratchet comparison.

    Parameters
    ----------
    argv : collections.abc.Sequence[str] | None
        Optional argument vector. When omitted, the process command line is
        parsed.

    Returns
    -------
    int
        ``0`` for pass/skip, ``1`` for regression, or ``2`` for invalid inputs.
    """
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    args = _parse_args(argv)
    try:
        report = _evaluate_ratchet(args)
        write_report(report=report, output_path=args.output)
    except IncompatibleBenchmarkProfileError as exc:
        write_incompatible_profile_report(
            reason=str(exc),
            decision=RatchetDecision(
                baseline_source=BaselineSource.NONE,
                baseline_reason=BaselineReason.INCOMPATIBLE_PROFILE,
                compatible_sample_count=0,
                comparison_state=ComparisonState.SKIPPED_INCOMPATIBLE_PROFILE,
            ),
            output_path=args.output,
        )
        _logger.info("benchmark ratchet skipped: %s", exc)
        return 0
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        _logger.exception("benchmark ratchet failed to evaluate inputs")
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
