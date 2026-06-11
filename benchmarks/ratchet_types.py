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
    """

    scenario_name: str
    baseline_ratio: float
    candidate_ratio: float
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
            "baseline_ratio": self.baseline_ratio,
            "candidate_ratio": self.candidate_ratio,
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
