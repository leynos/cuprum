"""Unit tests for pure benchmark-ratchet history models and statistics."""

from __future__ import annotations

import typing as typ

import pytest

from benchmarks.benchmark_profile import BENCHMARK_PROFILE_VERSION
from benchmarks.ratchet_history import BaselineHistory, HistorySample

SCENARIO = "medium-single-nocb"
WORKER_ITERATIONS = 20


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


class TestWindowMechanics:
    """The history model retains compatible, recent benchmark evidence."""

    def test_the_window_keeps_only_the_most_recent_samples(self) -> None:
        """Appending past the window drops the oldest sample."""
        updated = _history(1.0, 2.0, 3.0).appended(
            _sample(4.0, run_id="new"), window_size=3
        )
        assert [sample.ratios[SCENARIO] for sample in updated.samples] == [
            2.0,
            3.0,
            4.0,
        ], "appending must retain only the configured newest samples"

    def test_appending_rejects_a_window_smaller_than_one(self) -> None:
        """A zero-length window is rejected before discarding samples."""
        with pytest.raises(ValueError, match="window_size must be >= 1"):
            BaselineHistory().appended(_sample(1.0), window_size=0)

    def test_samples_from_an_older_profile_are_pruned(self) -> None:
        """A different sampling protocol records a different question."""
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
        assert [sample.ratios[SCENARIO] for sample in kept.samples] == [1.2], (
            "only samples measured with the current profile and iterations remain"
        )

    def test_the_window_expects_the_newest_sample_s_scenarios(self) -> None:
        """Candidates need not report a scenario the newest sample lacks."""
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
        assert history.scenarios == frozenset({SCENARIO}), (
            "the newest sample must define the required scenario shape"
        )
        assert history.ratios_for(SCENARIO) == (1.0, 1.0), (
            "active scenarios must retain all their recorded ratios"
        )
        assert history.ratios_for("retired-scenario") == (1.0,), (
            "retired scenarios remain available in older samples only"
        )


def test_history_sample_copies_and_freezes_its_ratios() -> None:
    """A caller cannot mutate a sample through its input mapping."""
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
    assert sample_from_ratios.ratios[SCENARIO] == pytest.approx(1.0), (
        "a history sample must copy rather than retain caller-owned ratios"
    )
    with pytest.raises(TypeError):
        typ.cast("dict[str, float]", sample_from_ratios.ratios)[SCENARIO] = 2.0
    assert isinstance(hash(sample), int), "a frozen sample must remain hashable"
