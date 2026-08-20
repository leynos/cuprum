"""Dataclasses for Rust benchmark ratchet comparison results."""

from __future__ import annotations

import dataclasses as dc

_FLOAT_TOLERANCE = 1e-12


@dc.dataclass(frozen=True, slots=True)
class ScenarioComparison:
    """Comparison result for one matched Rust/Python scenario pair.

    The baseline and candidate values are within-run ``rust_mean /
    python_mean`` ratios, so the regression ratio tracks how the Rust
    backend's relative performance changed rather than absolute wall-clock
    differences between runner machines.

    ``baseline_ratio`` is the median of the last ``baseline_sample_count``
    main-branch runs, and ``noise_tolerance`` is the spread those same runs
    exhibited, expressed relative to that median. A regression must clear
    both it and ``max_regression`` — the flat threshold alone cannot tell a
    real slowdown from a noisy runner, and the observed spread alone would
    let a genuinely regressed but consistent measurement through.

    The defaults describe a single-sample window with no measurable spread,
    which is what the pre-window ratchet compared against.
    """

    scenario_name: str
    baseline_ratio: float
    candidate_ratio: float
    regression_ratio: float
    max_regression: float
    baseline_sample_count: int = 1
    noise_tolerance: float = 0.0

    @property
    def effective_threshold(self) -> float:
        """Return the threshold this scenario is actually judged against."""
        return max(self.max_regression, self.noise_tolerance)

    @property
    def is_regression(self) -> bool:
        """Return ``True`` when scenario regression exceeds threshold."""
        return (self.regression_ratio - self.effective_threshold) > _FLOAT_TOLERANCE

    def as_dict(self) -> dict[str, object]:
        """Serialize the scenario comparison for JSON output.

        Returns
        -------
        dict[str, object]
            The scenario comparison fields as a JSON-ready mapping.
        """
        return {
            "scenario_name": self.scenario_name,
            "baseline_ratio": self.baseline_ratio,
            "baseline_sample_count": self.baseline_sample_count,
            "candidate_ratio": self.candidate_ratio,
            "regression_ratio": self.regression_ratio,
            "max_regression": self.max_regression,
            "noise_tolerance": self.noise_tolerance,
            "effective_threshold": self.effective_threshold,
            "is_regression": self.is_regression,
        }


@dc.dataclass(frozen=True, slots=True)
class ComparisonReport:
    """Summary report for Rust benchmark regression comparison."""

    max_regression: float
    comparisons: tuple[ScenarioComparison, ...]

    @property
    def baseline_sample_count(self) -> int:
        """Return the smallest window any compared scenario was judged against.

        Reported so a surprising verdict can be read against the evidence
        behind it: one sample is the old, noise-sensitive bar, and a full
        window is the intended one.
        """
        if not self.comparisons:
            return 0
        return min(comparison.baseline_sample_count for comparison in self.comparisons)

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
        """Serialize the report for JSON output.

        Returns
        -------
        dict[str, object]
            The report summary and per-scenario comparisons as a
            JSON-ready mapping.
        """
        return {
            "max_regression": self.max_regression,
            "baseline_sample_count": self.baseline_sample_count,
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
