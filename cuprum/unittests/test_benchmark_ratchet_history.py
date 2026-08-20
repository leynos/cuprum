"""Unit tests for the rolling baseline window and the noise-aware threshold.

The incident these tests encode: on 2026-08-06 a `main` run measured
`medium-single-nocb` at 0.760 against a baseline of 1.013, passed (a faster
measurement is never gated), and published that outlier as the next
baseline. Every pull request afterwards compared its perfectly ordinary
~1.11 against 0.760 and reported a 46% regression, three re-runs included.

Two properties have to hold for that not to recur, and they are independent:
the bar must not be one sample, and the samples must not be filtered by
whether their own run passed.
"""

from __future__ import annotations

import json
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from benchmarks.benchmark_profile import BENCHMARK_PROFILE_VERSION
from benchmarks.ratchet_history import (
    DEFAULT_NOISE_SIGMAS,
    DEFAULT_WINDOW_SIZE,
    MAX_NOISE_TOLERANCE,
    BaselineHistory,
    HistorySample,
    RatchetPolicy,
    history_from_payload,
    load_history,
    median_ratio,
    noise_tolerance,
    write_history,
)
from benchmarks.ratchet_rust_performance import compare_rust_regressions
from benchmarks.ratchet_types import BenchmarkRunPayload

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

SCENARIO = "medium-single-nocb"
WORKER_ITERATIONS = 20

#: The measurement that poisoned the baseline, and the ordinary pull-request
#: measurements that followed it. Both are real numbers from the incident.
OUTLIER_RATIO = 0.760
CANDIDATE_RATIO = 1.110
#: What `main` had been measuring before the outlier, and after it.
TYPICAL_RATIOS = (1.013, 1.001, 1.069, 0.916, 1.105)


def _sample(
    ratio: float,
    *,
    profile_version: str = BENCHMARK_PROFILE_VERSION,
    worker_iterations: int = WORKER_ITERATIONS,
    run_id: str = "1",
) -> HistorySample:
    """Return a history sample recording one ratio for `SCENARIO`."""
    return HistorySample(
        commit="0" * 40,
        run_id=run_id,
        benchmark_profile_version=profile_version,
        worker_iterations=worker_iterations,
        ratios={SCENARIO: ratio},
    )


def _history(*ratios: float) -> BaselineHistory:
    """Return a history of the given ratios, oldest first."""
    return BaselineHistory(
        samples=tuple(
            _sample(ratio, run_id=str(index)) for index, ratio in enumerate(ratios)
        )
    )


def _run(*, ratios: cabc.Mapping[str, float], context_name: str) -> BenchmarkRunPayload:
    """Return a benchmark run payload realizing the given per-scenario ratios.

    The ratio is what the ratchet reads, so the Python mean is fixed at one
    second and the Rust mean carries the ratio. Anything else would encode
    the same number twice.
    """
    scenarios: list[dict[str, object]] = []
    results: list[dict[str, object]] = []
    for scenario, ratio in sorted(ratios.items()):
        scenarios.extend((
            {"name": f"python-{scenario}", "backend": "python"},
            {"name": f"rust-{scenario}", "backend": "rust"},
        ))
        results.extend(({"mean": 1.0}, {"mean": ratio}))
    return BenchmarkRunPayload(
        plan={
            "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
            "worker_iterations": WORKER_ITERATIONS,
            "scenarios": scenarios,
        },
        throughput={"results": results},
        context_name=context_name,
    )


def _verdict(
    *,
    candidate_ratio: float,
    history: BaselineHistory,
    max_regression: float = 0.30,
) -> bool:
    """Return whether a candidate passes against a window."""
    report = compare_rust_regressions(
        candidate=_run(ratios={SCENARIO: candidate_ratio}, context_name="candidate"),
        history=history,
        policy=RatchetPolicy(max_regression=max_regression),
    )
    return report.passed


# -- The incident --------------------------------------------------------------


def test_one_noisy_main_sample_fails_the_next_pull_request() -> None:
    """A single-sample baseline reproduces the failure being fixed.

    Kept as the control: without it, the test below could pass because the
    numbers are benign rather than because the window did anything.
    """
    assert not _verdict(
        candidate_ratio=CANDIDATE_RATIO, history=_history(OUTLIER_RATIO)
    ), (
        "expected the incident to reproduce against a one-sample baseline: "
        f"{CANDIDATE_RATIO} against {OUTLIER_RATIO} is a 46% regression"
    )


def test_the_window_absorbs_one_noisy_main_sample() -> None:
    """The same candidate passes once the outlier is one sample among many."""
    history = _history(*TYPICAL_RATIOS, OUTLIER_RATIO)

    assert _verdict(candidate_ratio=CANDIDATE_RATIO, history=history), (
        f"a candidate of {CANDIDATE_RATIO} must pass against a window whose "
        f"other samples are {TYPICAL_RATIOS}; the {OUTLIER_RATIO} sample is "
        "one measurement, not a new bar"
    )


def test_a_real_regression_still_fails_against_a_noisy_window() -> None:
    """Tolerating noise must not mean tolerating a doubling."""
    history = _history(*TYPICAL_RATIOS, OUTLIER_RATIO)

    assert not _verdict(candidate_ratio=2.0, history=history), (
        "a candidate twice as slow as the window median must fail however "
        "wide the observed spread is"
    )


def test_a_sustained_regression_fails_before_it_enters_the_window() -> None:
    """A first regressed measurement fails while the window is still clean."""
    history = _history(*TYPICAL_RATIOS)

    assert not _verdict(candidate_ratio=1.60, history=history), (
        "a 55% slowdown against a window of consistent samples must fail; "
        "the noise band is narrow when the measurements agree"
    )


# -- Threshold composition -----------------------------------------------------


def test_the_flat_threshold_still_applies_to_a_wide_window() -> None:
    """The noise band widens the threshold; it never narrows it."""
    report = compare_rust_regressions(
        candidate=_run(ratios={SCENARIO: 1.0}, context_name="candidate"),
        history=_history(1.0, 1.0, 1.0),
        policy=RatchetPolicy(max_regression=0.30),
    )
    comparison = report.comparisons[0]

    assert comparison.noise_tolerance == pytest.approx(0.0), (
        "a window with no spread must contribute no tolerance"
    )
    assert comparison.effective_threshold == pytest.approx(0.30), (
        "with no observed spread the flat threshold decides alone, which is "
        "the pre-window behaviour"
    )


def test_the_observed_spread_can_widen_the_threshold() -> None:
    """A window that disagrees with itself must tolerate more, not less.

    The flat 30% alone would fail this candidate; the samples it is judged
    against span a far wider range than that, so the measurement cannot
    distinguish it from another noisy run.
    """
    history = _history(0.76, 1.40, 0.90, 1.30)
    report = compare_rust_regressions(
        candidate=_run(ratios={SCENARIO: 1.50}, context_name="candidate"),
        history=history,
        policy=RatchetPolicy(max_regression=0.30),
    )
    comparison = report.comparisons[0]

    assert comparison.regression_ratio > 0.30, (
        "this candidate must be one the flat threshold alone would reject, "
        "or the test proves nothing about the noise band"
    )
    assert comparison.effective_threshold > comparison.regression_ratio
    assert report.passed


def test_a_pathological_window_cannot_disable_the_ratchet() -> None:
    """The noise band is capped, so a hopeless window fails loudly, not open."""
    history = _history(0.5, 3.0, 0.6, 2.8)
    report = compare_rust_regressions(
        candidate=_run(ratios={SCENARIO: 20.0}, context_name="candidate"),
        history=history,
        policy=RatchetPolicy(max_regression=0.30),
    )
    comparison = report.comparisons[0]

    assert comparison.noise_tolerance == pytest.approx(MAX_NOISE_TOLERANCE)
    assert not report.passed, (
        "however noisy the window, a candidate an order of magnitude slower "
        "than its median must still fail"
    )


def test_a_single_sample_window_reports_its_own_weakness() -> None:
    """A one-sample comparison must say so in the report.

    A surprising verdict has to be readable against the evidence behind it,
    and one sample is the old, noise-sensitive bar.
    """
    report = compare_rust_regressions(
        candidate=_run(ratios={SCENARIO: 1.0}, context_name="candidate"),
        history=_history(1.0),
        policy=RatchetPolicy(max_regression=0.30),
    )

    assert report.baseline_sample_count == 1
    assert report.as_dict()["baseline_sample_count"] == 1


def test_noise_sigmas_zero_judges_on_the_flat_threshold_alone() -> None:
    """`--noise-sigmas 0` must restore the pre-window decision rule."""
    history = _history(*TYPICAL_RATIOS, OUTLIER_RATIO)
    report = compare_rust_regressions(
        candidate=_run(ratios={SCENARIO: CANDIDATE_RATIO}, context_name="candidate"),
        history=history,
        policy=RatchetPolicy(max_regression=0.30, noise_sigmas=0.0),
    )

    assert report.comparisons[0].noise_tolerance == pytest.approx(0.0)


# -- Window mechanics ----------------------------------------------------------


def test_the_window_keeps_only_the_most_recent_samples() -> None:
    """Appending past the window drops the oldest sample, not the newest."""
    history = _history(1.0, 2.0, 3.0)

    updated = history.appended(_sample(4.0, run_id="new"), window_size=3)

    assert [sample.ratios[SCENARIO] for sample in updated.samples] == [2.0, 3.0, 4.0]


def test_appending_rejects_a_window_smaller_than_one() -> None:
    """A zero-length window would silently discard every sample."""
    with pytest.raises(ValueError, match="window_size must be >= 1"):
        BaselineHistory().appended(_sample(1.0), window_size=0)


def test_samples_from_an_older_profile_are_pruned() -> None:
    """A different sampling protocol measures a different question."""
    history = BaselineHistory(
        samples=(
            _sample(1.0, profile_version="pipeline-worker-release-ratio-v2"),
            _sample(1.1, worker_iterations=WORKER_ITERATIONS + 1),
            _sample(1.2),
        )
    )

    kept = history.compatible_with(
        benchmark_profile_version=BENCHMARK_PROFILE_VERSION,
        worker_iterations=WORKER_ITERATIONS,
    )

    assert [sample.ratios[SCENARIO] for sample in kept.samples] == [1.2]


def test_an_empty_window_falls_back_to_the_single_sample_baseline() -> None:
    """A profile change must degrade to the old bar, not to no bar at all."""
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

    assert report.passed
    assert report.baseline_sample_count == 1, (
        "the fallback compares against exactly the one baseline run"
    )


def test_comparison_requires_some_baseline() -> None:
    """Neither a window nor a baseline is a programming error, not a pass."""
    with pytest.raises(ValueError, match="baseline run or a non-empty baseline"):
        compare_rust_regressions(
            candidate=_run(ratios={SCENARIO: 1.0}, context_name="candidate"),
            history=BaselineHistory(),
            policy=RatchetPolicy(max_regression=0.30),
        )


def test_the_window_expects_the_newest_sample_s_scenarios() -> None:
    """A scenario the newest sample lacks must not be demanded of candidates."""
    history = BaselineHistory(
        samples=(
            HistorySample(
                commit="0" * 40,
                run_id="1",
                benchmark_profile_version=BENCHMARK_PROFILE_VERSION,
                worker_iterations=WORKER_ITERATIONS,
                ratios={SCENARIO: 1.0, "retired-scenario": 1.0},
            ),
            _sample(1.0, run_id="2"),
        )
    )

    assert history.scenarios == frozenset({SCENARIO})
    assert history.ratios_for(SCENARIO) == (1.0, 1.0)
    assert history.ratios_for("retired-scenario") == (1.0,)


# -- Persistence ---------------------------------------------------------------


def test_history_round_trips_through_json(tmp_path: pth.Path) -> None:
    """A written window must read back identical."""
    history = _history(*TYPICAL_RATIOS)
    path = tmp_path / "main-baseline-history.json"

    write_history(history=history, output_path=path)

    assert load_history(path) == history


def test_an_absent_history_reads_as_empty(tmp_path: pth.Path) -> None:
    """A first run, or an expired artefact, is not an error."""
    assert load_history(tmp_path / "missing.json") == BaselineHistory()
    assert load_history(None) == BaselineHistory()


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("not json at all", "unparseable"),
        ('["not", "an", "object"]', "not a JSON object"),
        ('{"schema": 99, "samples": []}', "unrecognized schema"),
    ],
)
def test_an_unusable_history_reads_as_empty(
    tmp_path: pth.Path, content: str, reason: str
) -> None:
    """An unusable window degrades to the single-sample bar, not to a failure.

    The ratchet's job is to report regressions; refusing to run because a
    cached file is malformed reports nothing at all.
    """
    path = tmp_path / "main-baseline-history.json"
    path.write_text(content, encoding="utf-8")

    assert load_history(path) == BaselineHistory(), reason


def test_a_malformed_sample_is_an_error_not_a_silent_drop() -> None:
    """A recognized schema with a broken sample must not be read as empty.

    Skipping the sample would quietly narrow the window; the caller catches
    this and falls back with the reason logged.
    """
    payload = json.loads('{"schema": 1, "samples": [{"commit": "abc"}]}')

    with pytest.raises((TypeError, ValueError)):
        history_from_payload(payload)


# -- Properties ----------------------------------------------------------------

_RATIOS = st.floats(min_value=0.5, max_value=2.0, allow_nan=False, allow_infinity=False)


@given(
    values=st.lists(_RATIOS, min_size=1, max_size=9),
    candidate=_RATIOS,
    max_regression=st.floats(min_value=0.0, max_value=1.0),
)
def test_the_effective_threshold_never_undercuts_the_flat_one(
    values: list[float], candidate: float, max_regression: float
) -> None:
    """Observed noise may only widen the bar.

    A window that happened to be unusually consistent must not start failing
    changes the flat threshold would have allowed.
    """
    report = compare_rust_regressions(
        candidate=_run(ratios={SCENARIO: candidate}, context_name="candidate"),
        history=_history(*values),
        policy=RatchetPolicy(max_regression=max_regression),
    )
    comparison = report.comparisons[0]

    assert comparison.effective_threshold >= max_regression


@given(
    values=st.lists(_RATIOS, min_size=1, max_size=DEFAULT_WINDOW_SIZE),
    max_regression=st.floats(min_value=0.0, max_value=1.0),
)
def test_a_candidate_at_the_median_always_passes(
    values: list[float], max_regression: float
) -> None:
    """Measuring exactly what main measures is never a regression."""
    history = _history(*values)
    median = median_ratio(history.ratios_for(SCENARIO))

    assert _verdict(
        candidate_ratio=median, history=history, max_regression=max_regression
    )


def test_the_comparison_reads_no_more_than_the_window() -> None:
    """A longer history must be truncated to the window, newest kept.

    The recorder prunes as it appends, so this only bites when a window is
    shortened or a hand-edited artefact is read — but it decides which
    samples form the bar, so it is stated rather than assumed.
    """
    history = _history(*[1.0] * DEFAULT_WINDOW_SIZE, 5.0, 5.0)
    report = compare_rust_regressions(
        candidate=_run(ratios={SCENARIO: 5.0}, context_name="candidate"),
        history=history,
        policy=RatchetPolicy(max_regression=0.30),
    )
    comparison = report.comparisons[0]

    assert comparison.baseline_sample_count == DEFAULT_WINDOW_SIZE
    assert comparison.baseline_ratio == pytest.approx(1.0), (
        "the two newest samples plus the five 1.0s that fit the window put "
        "the median at 1.0"
    )


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
    typical: float, outlier: float, others: list[float]
) -> None:
    """A majority of agreeing samples outvotes any single measurement.

    This is the property the incident violated: one sample *was* the bar, so
    one sample decided every subsequent pull request.
    """
    agreeing = [typical * factor for factor in others]
    history = _history(*agreeing, outlier)

    assert _verdict(candidate_ratio=typical, history=history), (
        f"a candidate of {typical} must pass against {len(agreeing)} samples "
        f"agreeing on it, whatever the single {outlier} sample says"
    )


@given(values=st.lists(_RATIOS, min_size=2, max_size=9), sigmas=_RATIOS)
def test_noise_tolerance_scales_with_the_requested_sigmas(
    values: list[float], sigmas: float
) -> None:
    """Asking for a wider band must not produce a narrower one."""
    narrow = noise_tolerance(values, sigmas=sigmas)
    wide = noise_tolerance(values, sigmas=sigmas * 2)

    assert wide >= narrow


def test_noise_tolerance_needs_at_least_two_samples() -> None:
    """One measurement has no spread to estimate."""
    assert noise_tolerance([1.0], sigmas=DEFAULT_NOISE_SIGMAS) == pytest.approx(0.0)
    assert noise_tolerance([], sigmas=DEFAULT_NOISE_SIGMAS) == pytest.approx(0.0)
