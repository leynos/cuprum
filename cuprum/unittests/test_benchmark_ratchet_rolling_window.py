"""Unit tests for rolling benchmark-ratchet comparison behaviour."""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from benchmarks.ratchet_history import (
    DEFAULT_WINDOW_SIZE,
    BaselineHistory,
    RatchetPolicy,
    median_ratio,
)
from benchmarks.ratchet_rust_performance import compare_rust_regressions
from benchmarks.ratchet_types import BenchmarkRunPayload
from cuprum.unittests.conftest import (
    SCENARIO,
    TYPICAL_RATIOS,
    WORKER_ITERATIONS,
    _history,
    benchmark_run_payloads,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

OUTLIER_RATIO = 0.760
CANDIDATE_RATIO = 1.110
_RATIOS = st.floats(min_value=0.5, max_value=2.0, allow_nan=False, allow_infinity=False)


def _run(*, ratios: cabc.Mapping[str, float], context_name: str) -> BenchmarkRunPayload:
    """Return a payload with the requested within-run ratios."""
    plan, throughput = benchmark_run_payloads(
        ratios, worker_iterations=WORKER_ITERATIONS
    )
    return BenchmarkRunPayload(
        plan=plan, throughput=throughput, context_name=context_name
    )


def _verdict(
    *, candidate_ratio: float, history: BaselineHistory, max_regression: float = 0.30
) -> bool:
    """Return whether one candidate passes against a history window."""
    return compare_rust_regressions(
        candidate=_run(ratios={SCENARIO: candidate_ratio}, context_name="candidate"),
        history=history,
        policy=RatchetPolicy(max_regression=max_regression),
    ).passed


class TestIncident:
    """The outlier incident the rolling window must prevent."""

    def test_one_noisy_main_sample_fails_the_next_pull_request(self) -> None:
        """A one-sample bar reproduces the incident's false regression."""
        assert not _verdict(
            candidate_ratio=CANDIDATE_RATIO, history=_history(OUTLIER_RATIO)
        ), (
            "expected the incident to reproduce against a one-sample baseline: "
            f"{CANDIDATE_RATIO} against {OUTLIER_RATIO} is a 46% regression"
        )

    def test_the_window_absorbs_one_noisy_main_sample(self) -> None:
        """The same candidate passes when the outlier is only one sample."""
        history = _history(*TYPICAL_RATIOS, OUTLIER_RATIO)
        assert _verdict(candidate_ratio=CANDIDATE_RATIO, history=history), (
            f"a candidate of {CANDIDATE_RATIO} must pass against a window whose "
            f"other samples are {TYPICAL_RATIOS}; the {OUTLIER_RATIO} sample is "
            "one measurement, not a new bar"
        )

    def test_a_real_regression_still_fails_against_a_noisy_window(self) -> None:
        """Tolerating noise does not tolerate a doubling."""
        assert not _verdict(
            candidate_ratio=2.0, history=_history(*TYPICAL_RATIOS, OUTLIER_RATIO)
        ), (
            "a candidate twice as slow as the window median must fail however "
            "wide the spread"
        )

    def test_a_sustained_regression_fails_before_it_enters_the_window(self) -> None:
        """The first sustained regression fails while the window remains clean."""
        assert not _verdict(candidate_ratio=1.60, history=_history(*TYPICAL_RATIOS)), (
            "a 55% slowdown against a window of consistent samples must fail; "
            "the noise band is narrow when the measurements agree"
        )


class TestRollingWindow:
    """Fallback and truncation behaviours of the comparison window."""

    def test_an_empty_window_falls_back_to_the_single_sample_baseline(self) -> None:
        """A profile change falls back to the old bar rather than no bar."""
        history = _history(0.5).compatible_with(
            benchmark_profile_version="pipeline-worker-release-ratio-v2",
            worker_iterations=WORKER_ITERATIONS,
        )
        report = compare_rust_regressions(
            baseline=_run(ratios={SCENARIO: 1.0}, context_name="baseline"),
            candidate=_run(ratios={SCENARIO: 1.0}, context_name="candidate"),
            history=history,
            policy=RatchetPolicy(max_regression=0.30),
        )
        assert report.passed, "a compatible fallback baseline must be comparable"
        assert report.baseline_sample_count == 1, (
            "the fallback compares against exactly the one baseline run"
        )

    def test_comparison_requires_some_baseline(self) -> None:
        """Neither a window nor fallback baseline is a pass."""
        with pytest.raises(ValueError, match="baseline run or a non-empty baseline"):
            compare_rust_regressions(
                candidate=_run(ratios={SCENARIO: 1.0}, context_name="candidate"),
                history=BaselineHistory(),
                policy=RatchetPolicy(max_regression=0.30),
            )

    def test_the_comparison_reads_no_more_than_the_window(self) -> None:
        """A long history is truncated to the newest configured window."""
        report = compare_rust_regressions(
            candidate=_run(ratios={SCENARIO: 5.0}, context_name="candidate"),
            history=_history(*[1.0] * DEFAULT_WINDOW_SIZE, 5.0, 5.0),
            policy=RatchetPolicy(max_regression=0.30),
        )
        comparison = report.comparisons[0]
        assert comparison.baseline_sample_count == DEFAULT_WINDOW_SIZE, (
            "the comparison must use no more than the configured history window"
        )
        assert comparison.baseline_ratio == pytest.approx(1.0), (
            "the two newest samples plus the five 1.0s that fit the window put "
            "the median at 1.0"
        )


class TestRollingWindowProperties:
    """Properties that must hold across windows and candidate measurements."""

    @given(
        values=st.lists(_RATIOS, min_size=1, max_size=9),
        candidate=_RATIOS,
        max_regression=st.floats(min_value=0.0, max_value=1.0),
    )
    def test_the_effective_threshold_never_undercuts_the_flat_one(
        self, values: list[float], candidate: float, max_regression: float
    ) -> None:
        """Observed noise can only widen the bar."""
        report = compare_rust_regressions(
            candidate=_run(ratios={SCENARIO: candidate}, context_name="candidate"),
            history=_history(*values),
            policy=RatchetPolicy(max_regression=max_regression),
        )
        assert report.comparisons[0].effective_threshold >= max_regression, (
            "the effective threshold must never undercut the flat threshold"
        )

    @given(
        values=st.lists(_RATIOS, min_size=1, max_size=DEFAULT_WINDOW_SIZE),
        max_regression=st.floats(min_value=0.0, max_value=1.0),
    )
    def test_a_candidate_at_the_median_always_passes(
        self, values: list[float], max_regression: float
    ) -> None:
        """Measuring exactly what main measures is not a regression."""
        history = _history(*values)
        assert _verdict(
            candidate_ratio=median_ratio(history.ratios_for(SCENARIO)),
            history=history,
            max_regression=max_regression,
        ), "a candidate at the baseline median must pass"

    @given(
        typical=st.floats(min_value=0.8, max_value=1.2),
        outlier=_RATIOS,
        others=st.lists(
            st.floats(min_value=0.99, max_value=1.01),
            min_size=4,
            max_size=DEFAULT_WINDOW_SIZE - 1,
        ),
    )
    def test_one_arbitrary_sample_cannot_move_the_verdict(
        self, typical: float, outlier: float, others: list[float]
    ) -> None:
        """A majority of agreeing samples outvotes one measurement."""
        agreeing = [typical * factor for factor in others]
        assert _verdict(
            candidate_ratio=typical, history=_history(*agreeing, outlier)
        ), (
            f"a candidate of {typical} must pass against {len(agreeing)} samples "
            f"agreeing on it, whatever the single {outlier} sample says"
        )
