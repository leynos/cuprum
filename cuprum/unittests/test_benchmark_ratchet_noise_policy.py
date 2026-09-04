"""Unit tests for benchmark-ratchet noise-policy thresholds."""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from benchmarks.ratchet_history import (
    DEFAULT_NOISE_SIGMAS,
    MAX_NOISE_TOLERANCE,
    RatchetPolicy,
    median_ratio,
    noise_tolerance,
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


class TestThresholdComposition:
    """The flat and observed-noise thresholds compose without failing open."""

    def test_the_flat_threshold_still_applies_to_a_wide_window(self) -> None:
        """The flat threshold decides when a window has no spread."""
        comparison = compare_rust_regressions(
            candidate=_run(ratios={SCENARIO: 1.0}, context_name="candidate"),
            history=_history(1.0, 1.0, 1.0),
            policy=RatchetPolicy(max_regression=0.30),
        ).comparisons[0]
        assert comparison.noise_tolerance == pytest.approx(0.0), (
            "a window with no spread must contribute no tolerance"
        )
        assert comparison.effective_threshold == pytest.approx(0.30), (
            "with no observed spread the flat threshold decides alone, which is "
            "the pre-window behaviour"
        )

    def test_the_observed_spread_can_widen_the_threshold(self) -> None:
        """A noisy window tolerates more without ever narrowing the threshold."""
        report = compare_rust_regressions(
            candidate=_run(ratios={SCENARIO: 1.50}, context_name="candidate"),
            history=_history(0.76, 1.40, 0.90, 1.30),
            policy=RatchetPolicy(max_regression=0.30),
        )
        comparison = report.comparisons[0]
        assert comparison.regression_ratio > 0.30, (
            "this candidate must be one the flat threshold alone would reject, "
            "or the test proves nothing about the noise band"
        )
        assert comparison.effective_threshold > comparison.regression_ratio, (
            "observed noise must widen the bar beyond this candidate's slowdown"
        )
        assert report.passed, "a candidate inside the widened noise bar must pass"

    def test_the_mad_noise_band_is_serialized_with_its_exact_threshold(self) -> None:
        """The report exposes the scaled-MAD calculation behind its verdict."""
        history = _history(1.0, 2.0, 3.0)
        comparison = (
            compare_rust_regressions(
                candidate=_run(ratios={SCENARIO: 3.4}, context_name="candidate"),
                history=history,
                policy=RatchetPolicy(max_regression=0.30, noise_sigmas=1.0),
            )
            .comparisons[0]
            .as_dict()
        )
        assert median_ratio(history.ratios_for(SCENARIO)) == pytest.approx(2.0), (
            "the exact MAD example has median ratio 2.0"
        )
        assert noise_tolerance(
            history.ratios_for(SCENARIO), sigmas=1.0
        ) == pytest.approx(1.4826 / 2.0), (
            "the exact MAD example must retain the scaled tolerance"
        )
        assert comparison["noise_tolerance"] == pytest.approx(1.4826 / 2.0), (
            "the serialized noise band must use the 1.4826 MAD scale"
        )
        assert comparison["effective_threshold"] == pytest.approx(1.4826 / 2.0), (
            "the serialized threshold must select the wider MAD noise band"
        )

    def test_the_mad_noise_band_caps_at_one(self) -> None:
        """A very wide window cannot silently disable the ratchet."""
        assert noise_tolerance([1.0, 2.0, 3.0], sigmas=3.0) == pytest.approx(1.0), (
            "the MAD-derived noise band must respect its hard cap"
        )

    def test_a_pathological_window_cannot_disable_the_ratchet(self) -> None:
        """The capped noise band still rejects a hopeless measurement."""
        report = compare_rust_regressions(
            candidate=_run(ratios={SCENARIO: 20.0}, context_name="candidate"),
            history=_history(0.5, 3.0, 0.6, 2.8),
            policy=RatchetPolicy(max_regression=0.30),
        )
        assert report.comparisons[0].noise_tolerance == pytest.approx(
            MAX_NOISE_TOLERANCE
        ), "the observed tolerance must cap at the policy maximum"
        assert not report.passed, (
            "however noisy the window, a candidate an order of magnitude slower "
            "than its median must still fail"
        )

    def test_a_single_sample_window_reports_its_own_weakness(self) -> None:
        """A one-sample comparison reports its own limited evidence."""
        report = compare_rust_regressions(
            candidate=_run(ratios={SCENARIO: 1.0}, context_name="candidate"),
            history=_history(1.0),
            policy=RatchetPolicy(max_regression=0.30),
        )
        assert report.baseline_sample_count == 1, (
            "a one-sample history must report its actual evidence count"
        )
        assert report.as_dict()["baseline_sample_count"] == 1, (
            "the serialized report must retain its evidence count"
        )

    def test_noise_sigmas_zero_judges_on_the_flat_threshold_alone(self) -> None:
        """`--noise-sigmas 0` restores the pre-window decision rule."""
        report = compare_rust_regressions(
            candidate=_run(
                ratios={SCENARIO: CANDIDATE_RATIO}, context_name="candidate"
            ),
            history=_history(*TYPICAL_RATIOS, OUTLIER_RATIO),
            policy=RatchetPolicy(max_regression=0.30, noise_sigmas=0.0),
        )
        assert report.comparisons[0].noise_tolerance == pytest.approx(0.0), (
            "zero requested sigmas must suppress the observed-noise tolerance"
        )


class TestNoiseToleranceProperties:
    """Properties of the robust observed-noise calculation."""

    @given(values=st.lists(_RATIOS, min_size=2, max_size=9), sigmas=_RATIOS)
    def test_noise_tolerance_scales_with_the_requested_sigmas(
        self, values: list[float], sigmas: float
    ) -> None:
        """A wider requested band cannot yield a narrower tolerance."""
        assert noise_tolerance(values, sigmas=sigmas * 2) >= noise_tolerance(
            values, sigmas=sigmas
        ), "increasing requested sigmas must not shrink the tolerance"

    def test_noise_tolerance_needs_at_least_two_samples(self) -> None:
        """One measurement has no spread to estimate."""
        assert noise_tolerance([1.0], sigmas=DEFAULT_NOISE_SIGMAS) == pytest.approx(
            0.0
        ), "a one-sample window has no observed spread"
        assert noise_tolerance([], sigmas=DEFAULT_NOISE_SIGMAS) == pytest.approx(0.0), (
            "an empty window has no observed spread"
        )
