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
    BaselineHistoryNotFoundError,
    BaselineHistoryReadError,
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

    Returns
    -------
    BenchmarkRunPayload
        A plan and matching Hyperfine results for the requested ratios.
    """
    scenarios: list[dict[str, object]] = []
    results: list[dict[str, object]] = []
    for scenario, ratio in sorted(ratios.items()):
        python_scenario: dict[str, object] = {
            "name": f"python-{scenario}",
            "backend": "python",
        }
        rust_scenario: dict[str, object] = {
            "name": f"rust-{scenario}",
            "backend": "rust",
        }
        scenarios.extend((python_scenario, rust_scenario))
        python_result: dict[str, object] = {
            "command": f"python-{scenario}",
            "mean": 1.0,
        }
        rust_result: dict[str, object] = {"command": f"rust-{scenario}", "mean": ratio}
        results.extend((python_result, rust_result))
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


class TestIncident:
    """The outlier incident the rolling window must prevent."""

    def test_one_noisy_main_sample_fails_the_next_pull_request(self) -> None:
        """Verify a single-sample baseline reproduces the failure being fixed.

        Kept as the control: without it, the test below could pass because the
        numbers are benign rather than because the window did anything.
        """
        assert not _verdict(
            candidate_ratio=CANDIDATE_RATIO, history=_history(OUTLIER_RATIO)
        ), (
            "expected the incident to reproduce against a one-sample baseline: "
            f"{CANDIDATE_RATIO} against {OUTLIER_RATIO} is a 46% regression"
        )

    def test_the_window_absorbs_one_noisy_main_sample(self) -> None:
        """Verify the candidate passes once the outlier is one sample among many."""
        history = _history(*TYPICAL_RATIOS, OUTLIER_RATIO)

        assert _verdict(candidate_ratio=CANDIDATE_RATIO, history=history), (
            f"a candidate of {CANDIDATE_RATIO} must pass against a window whose "
            f"other samples are {TYPICAL_RATIOS}; the {OUTLIER_RATIO} sample is "
            "one measurement, not a new bar"
        )

    def test_a_real_regression_still_fails_against_a_noisy_window(self) -> None:
        """Tolerating noise must not mean tolerating a doubling."""
        history = _history(*TYPICAL_RATIOS, OUTLIER_RATIO)

        assert not _verdict(candidate_ratio=2.0, history=history), (
            "a candidate twice as slow as the window median must fail however "
            "wide the observed spread is"
        )

    def test_a_sustained_regression_fails_before_it_enters_the_window(self) -> None:
        """Verify a first regressed measurement fails while the window is clean."""
        history = _history(*TYPICAL_RATIOS)

        assert not _verdict(candidate_ratio=1.60, history=history), (
            "a 55% slowdown against a window of consistent samples must fail; "
            "the noise band is narrow when the measurements agree"
        )


# -- Threshold composition -----------------------------------------------------


def _test_the_flat_threshold_still_applies_to_a_wide_window() -> None:
    """Verify the noise band widens the threshold without narrowing it."""
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


def _test_the_observed_spread_can_widen_the_threshold() -> None:
    """Verify a window that disagrees with itself tolerates more, not less.

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
    assert comparison.effective_threshold > comparison.regression_ratio, (
        "observed noise must widen the bar beyond this candidate's slowdown"
    )
    assert report.passed, "a candidate inside the widened noise bar must pass"


def _test_the_mad_noise_band_is_serialized_with_its_exact_threshold() -> None:
    """Verify the report exposes the scaled-MAD calculation behind its verdict."""
    history = _history(1.0, 2.0, 3.0)
    report = compare_rust_regressions(
        candidate=_run(ratios={SCENARIO: 3.4}, context_name="candidate"),
        history=history,
        policy=RatchetPolicy(max_regression=0.30, noise_sigmas=1.0),
    )
    comparison = report.comparisons[0].as_dict()

    assert median_ratio(history.ratios_for(SCENARIO)) == pytest.approx(2.0), (
        "the exact MAD example has median ratio 2.0"
    )
    assert noise_tolerance(history.ratios_for(SCENARIO), sigmas=1.0) == pytest.approx(
        1.4826 / 2.0
    )
    assert comparison["noise_tolerance"] == pytest.approx(1.4826 / 2.0), (
        "the serialized noise band must use the 1.4826 MAD scale"
    )
    assert comparison["effective_threshold"] == pytest.approx(1.4826 / 2.0), (
        "the serialized threshold must select the wider MAD noise band"
    )


def _test_the_mad_noise_band_caps_at_one() -> None:
    """Verify a very wide window cannot silently disable the ratchet."""
    assert noise_tolerance([1.0, 2.0, 3.0], sigmas=3.0) == pytest.approx(1.0)


def _test_a_pathological_window_cannot_disable_the_ratchet() -> None:
    """Verify the capped noise band still rejects a hopeless measurement."""
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


def _test_a_single_sample_window_reports_its_own_weakness() -> None:
    """Verify a one-sample comparison says so in the report.

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


def _test_noise_sigmas_zero_judges_on_the_flat_threshold_alone() -> None:
    """`--noise-sigmas 0` must restore the pre-window decision rule."""
    history = _history(*TYPICAL_RATIOS, OUTLIER_RATIO)
    report = compare_rust_regressions(
        candidate=_run(ratios={SCENARIO: CANDIDATE_RATIO}, context_name="candidate"),
        history=history,
        policy=RatchetPolicy(max_regression=0.30, noise_sigmas=0.0),
    )

    assert report.comparisons[0].noise_tolerance == pytest.approx(0.0)


class TestThresholdComposition:
    """The flat and observed-noise thresholds compose without failing open."""

    def test_the_flat_threshold_still_applies_to_a_wide_window(self) -> None:
        """Run the flat-threshold composition check."""
        _test_the_flat_threshold_still_applies_to_a_wide_window()

    def test_the_observed_spread_can_widen_the_threshold(self) -> None:
        """Run the observed-spread composition check."""
        _test_the_observed_spread_can_widen_the_threshold()

    def test_the_mad_noise_band_is_serialized_with_its_exact_threshold(self) -> None:
        """Run the serialized scaled-MAD check."""
        _test_the_mad_noise_band_is_serialized_with_its_exact_threshold()

    def test_the_mad_noise_band_caps_at_one(self) -> None:
        """Run the maximum-noise-band check."""
        _test_the_mad_noise_band_caps_at_one()

    def test_a_pathological_window_cannot_disable_the_ratchet(self) -> None:
        """Run the pathological-window check."""
        _test_a_pathological_window_cannot_disable_the_ratchet()

    def test_a_single_sample_window_reports_its_own_weakness(self) -> None:
        """Run the baseline-sample-count check."""
        _test_a_single_sample_window_reports_its_own_weakness()

    def test_noise_sigmas_zero_judges_on_the_flat_threshold_alone(self) -> None:
        """Run the no-observed-noise check."""
        _test_noise_sigmas_zero_judges_on_the_flat_threshold_alone()


# -- Window mechanics ----------------------------------------------------------


def _test_the_window_keeps_only_the_most_recent_samples() -> None:
    """Verify appending past the window drops the oldest sample."""
    history = _history(1.0, 2.0, 3.0)

    updated = history.appended(_sample(4.0, run_id="new"), window_size=3)

    assert [sample.ratios[SCENARIO] for sample in updated.samples] == [2.0, 3.0, 4.0]


def _test_appending_rejects_a_window_smaller_than_one() -> None:
    """Verify a zero-length window is rejected before discarding samples."""
    with pytest.raises(ValueError, match="window_size must be >= 1"):
        BaselineHistory().appended(_sample(1.0), window_size=0)


def _test_samples_from_an_older_profile_are_pruned() -> None:
    """Verify a different sampling protocol records a different question."""
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


def _test_an_empty_window_falls_back_to_the_single_sample_baseline() -> None:
    """Verify a profile change falls back to the old bar, not no bar."""
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


def _test_comparison_requires_some_baseline() -> None:
    """Neither a window nor a baseline is a programming error, not a pass."""
    with pytest.raises(ValueError, match="baseline run or a non-empty baseline"):
        compare_rust_regressions(
            candidate=_run(ratios={SCENARIO: 1.0}, context_name="candidate"),
            history=BaselineHistory(),
            policy=RatchetPolicy(max_regression=0.30),
        )


def _test_the_window_expects_the_newest_sample_s_scenarios() -> None:
    """Verify candidates need not report a scenario the newest sample lacks."""
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


class TestWindowMechanics:
    """The window retains comparable, recent benchmark evidence."""

    def test_the_window_keeps_only_the_most_recent_samples(self) -> None:
        """Run the rolling-window retention check."""
        _test_the_window_keeps_only_the_most_recent_samples()

    def test_appending_rejects_a_window_smaller_than_one(self) -> None:
        """Run the invalid-window check."""
        _test_appending_rejects_a_window_smaller_than_one()

    def test_samples_from_an_older_profile_are_pruned(self) -> None:
        """Run the incompatible-profile pruning check."""
        _test_samples_from_an_older_profile_are_pruned()

    def test_an_empty_window_falls_back_to_the_single_sample_baseline(self) -> None:
        """Run the empty-window fallback check."""
        _test_an_empty_window_falls_back_to_the_single_sample_baseline()

    def test_comparison_requires_some_baseline(self) -> None:
        """Run the no-baseline error check."""
        _test_comparison_requires_some_baseline()

    def test_the_window_expects_the_newest_sample_s_scenarios(self) -> None:
        """Run the newest-sample scenario-shape check."""
        _test_the_window_expects_the_newest_sample_s_scenarios()


# -- Persistence ---------------------------------------------------------------


def _test_history_round_trips_through_json(tmp_path: pth.Path) -> None:
    """Verify a written window reads back identically."""
    history = _history(*TYPICAL_RATIOS)
    path = tmp_path / "main-baseline-history.json"

    write_history(history=history, output_path=path)

    assert load_history(path) == history


def _test_history_sample_copies_and_freezes_its_ratios() -> None:
    """Verify a caller cannot mutate a sample through its input mapping."""
    ratios = {SCENARIO: 1.0}
    sample = _sample(1.0)
    sample_from_ratios = HistorySample(
        commit="0" * 40,
        run_id="immutable",
        benchmark_profile_version=BENCHMARK_PROFILE_VERSION,
        worker_iterations=WORKER_ITERATIONS,
        ratios=ratios,
    )
    ratios[SCENARIO] = 2.0

    assert sample_from_ratios.ratios[SCENARIO] == pytest.approx(1.0)
    with pytest.raises(TypeError):
        typ.cast("dict[str, float]", sample_from_ratios.ratios)[SCENARIO] = 2.0
    assert isinstance(hash(sample), int)


def _test_an_unusable_history_raises_a_typed_error(
    tmp_path: pth.Path, content: str, reason: str
) -> None:
    """Corrupt persisted state is distinct from an intentionally absent file."""
    path = tmp_path / "main-baseline-history.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(BaselineHistoryReadError, match=reason) as error:
        load_history(path)
    assert error.value.path == path, "the typed error must retain its source path"
    assert reason in error.value.reason, "the typed error must retain its reason"


def _test_a_missing_history_has_a_distinct_typed_error(tmp_path: pth.Path) -> None:
    """Verify an absent optional artefact has a distinct error."""
    path = tmp_path / "main-baseline-history.json"

    with pytest.raises(BaselineHistoryNotFoundError) as error:
        load_history(path)

    assert error.value.path == path, "the not-found error must retain its path"
    assert error.value.reason == "does not exist", (
        "the not-found error must expose its stable reason"
    )


def _test_a_malformed_sample_is_an_error_not_a_silent_drop() -> None:
    """Verify a recognized schema with a broken sample is not read as empty.

    Skipping the sample would quietly narrow the window; the caller catches
    this and falls back with the reason logged.
    """
    payload = json.loads(
        """{
        "schema": 1,
        "samples": [{
            "commit": "abc",
            "benchmark_profile_version": "profile",
            "worker_iterations": 1,
            "ratios": {"medium-single-nocb": 1.0}
        }]
        }"""
    )

    with pytest.raises(TypeError, match=r"samples\[0\]\.run_id"):
        history_from_payload(payload)


def _test_a_failed_history_replacement_preserves_the_previous_file(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify failed publication cannot truncate the last complete history."""
    path = tmp_path / "main-baseline-history.json"
    existing = _history(1.0)
    write_history(history=existing, output_path=path)

    class _ReplaceError(OSError):
        """A simulated failure replacing the published history."""

    def _replace_fails(source: pth.Path, destination: pth.Path) -> None:
        """Simulate a failure after the temporary history is fully written."""
        del source, destination
        raise _ReplaceError

    monkeypatch.setattr(type(path), "replace", _replace_fails)

    with pytest.raises(_ReplaceError):
        write_history(history=_history(2.0), output_path=path)

    assert load_history(path) == existing, (
        "a failed replacement must preserve the last complete history"
    )
    assert not list(tmp_path.glob(".main-baseline-history.json.*")), (
        "a failed replacement must remove its unpublished temporary history"
    )


def _test_a_failed_history_sync_removes_the_temporary_file(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that a write-stage failure leaves no temporary history file."""
    path = tmp_path / "main-baseline-history.json"

    def _sync_fails(_: int) -> None:
        """Simulate storage refusing to persist the temporary payload."""
        error = OSError("simulated fsync failure")
        raise error

    monkeypatch.setattr("benchmarks.ratchet_history.os.fsync", _sync_fails)

    with pytest.raises(OSError, match="simulated fsync failure"):
        write_history(history=_history(2.0), output_path=path)

    assert not list(tmp_path.glob(".main-baseline-history.json.*")), (
        "a failed temporary-file sync must remove the unpublished history file"
    )


class TestPersistence:
    """Persisted history stays immutable, validated, and atomically replaceable."""

    def test_history_round_trips_through_json(self, tmp_path: pth.Path) -> None:
        """Run the JSON history round-trip check."""
        _test_history_round_trips_through_json(tmp_path)

    def test_history_sample_copies_and_freezes_its_ratios(self) -> None:
        """Run the immutable-ratios check."""
        _test_history_sample_copies_and_freezes_its_ratios()

    @pytest.mark.parametrize(
        ("content", "reason"),
        [
            ("not json at all", "could not read"),
            ('["not", "an", "object"]', "must contain a JSON object"),
            ('{"schema": 99, "samples": []}', "schema must be"),
        ],
    )
    def test_an_unusable_history_raises_a_typed_error(
        self, tmp_path: pth.Path, content: str, reason: str
    ) -> None:
        """Run the malformed-history type and diagnostics check."""
        _test_an_unusable_history_raises_a_typed_error(tmp_path, content, reason)

    def test_a_malformed_sample_is_an_error_not_a_silent_drop(self) -> None:
        """Run the malformed-sample rejection check."""
        _test_a_malformed_sample_is_an_error_not_a_silent_drop()

    def test_a_missing_history_has_a_distinct_typed_error(
        self, tmp_path: pth.Path
    ) -> None:
        """Run the missing-history type check."""
        _test_a_missing_history_has_a_distinct_typed_error(tmp_path)

    def test_a_failed_history_replacement_preserves_the_previous_file(
        self, tmp_path: pth.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Run the failed-replacement preservation check."""
        _test_a_failed_history_replacement_preserves_the_previous_file(
            tmp_path, monkeypatch
        )

    def test_a_failed_history_sync_removes_the_temporary_file(
        self, tmp_path: pth.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Run the failed-sync cleanup check."""
        _test_a_failed_history_sync_removes_the_temporary_file(tmp_path, monkeypatch)


# -- Properties ----------------------------------------------------------------

_RATIOS = st.floats(min_value=0.5, max_value=2.0, allow_nan=False, allow_infinity=False)


def _test_the_comparison_reads_no_more_than_the_window() -> None:
    """Verify a longer history is truncated to the newest window.

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

    assert comparison.baseline_sample_count == DEFAULT_WINDOW_SIZE, (
        "the comparison must use no more than the configured history window"
    )
    assert comparison.baseline_ratio == pytest.approx(1.0), (
        "the two newest samples plus the five 1.0s that fit the window put "
        "the median at 1.0"
    )


def _test_noise_tolerance_needs_at_least_two_samples() -> None:
    """One measurement has no spread to estimate."""
    assert noise_tolerance([1.0], sigmas=DEFAULT_NOISE_SIGMAS) == pytest.approx(0.0), (
        "a one-sample window has no observed spread"
    )
    assert noise_tolerance([], sigmas=DEFAULT_NOISE_SIGMAS) == pytest.approx(0.0), (
        "an empty window has no observed spread"
    )


class TestProperties:
    """Properties that must hold across windows and candidate measurements."""

    @given(
        values=st.lists(_RATIOS, min_size=1, max_size=9),
        candidate=_RATIOS,
        max_regression=st.floats(min_value=0.0, max_value=1.0),
    )
    def test_the_effective_threshold_never_undercuts_the_flat_one(
        self, values: list[float], candidate: float, max_regression: float
    ) -> None:
        """Verify observed noise can only widen the bar."""
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
        """Verify measuring exactly what main measures is not a regression."""
        history = _history(*values)
        median = median_ratio(history.ratios_for(SCENARIO))
        assert _verdict(
            candidate_ratio=median, history=history, max_regression=max_regression
        ), "a candidate at the baseline median must pass"

    def test_the_comparison_reads_no_more_than_the_window(self) -> None:
        """Run the history-window truncation check."""
        _test_the_comparison_reads_no_more_than_the_window()

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
        """Verify a majority of agreeing samples outvotes one measurement."""
        agreeing = [typical * factor for factor in others]
        history = _history(*agreeing, outlier)
        assert _verdict(candidate_ratio=typical, history=history), (
            f"a candidate of {typical} must pass against {len(agreeing)} samples "
            f"agreeing on it, whatever the single {outlier} sample says"
        )

    @given(values=st.lists(_RATIOS, min_size=2, max_size=9), sigmas=_RATIOS)
    def test_noise_tolerance_scales_with_the_requested_sigmas(
        self, values: list[float], sigmas: float
    ) -> None:
        """Verify a wider requested band cannot yield a narrower tolerance."""
        assert noise_tolerance(values, sigmas=sigmas * 2) >= noise_tolerance(
            values, sigmas=sigmas
        )

    def test_noise_tolerance_needs_at_least_two_samples(self) -> None:
        """Run the minimum-spread-sample check."""
        _test_noise_tolerance_needs_at_least_two_samples()
