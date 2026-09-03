"""Unit tests for benchmark-ratchet history models and persistence."""

from __future__ import annotations

import json
import typing as typ

import pytest

from benchmarks.benchmark_profile import BENCHMARK_PROFILE_VERSION
from benchmarks.ratchet_history import (
    BaselineHistory,
    BaselineHistoryNotFoundError,
    BaselineHistoryReadError,
    HistorySample,
    history_from_payload,
    load_history,
    write_history,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

SCENARIO = "medium-single-nocb"
WORKER_ITERATIONS = 20
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


class TestPersistence:
    """Persisted history stays immutable, validated, and atomically replaceable."""

    def test_history_round_trips_through_json(self, tmp_path: pth.Path) -> None:
        """A written window reads back identically."""
        history = _history(*TYPICAL_RATIOS)
        path = tmp_path / "main-baseline-history.json"
        write_history(history=history, output_path=path)
        assert load_history(path) == history, "written history must round-trip exactly"

    def test_history_sample_copies_and_freezes_its_ratios(self) -> None:
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
        """Corrupt persisted state is distinct from an intentionally absent file."""
        path = tmp_path / "main-baseline-history.json"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(BaselineHistoryReadError, match=reason) as error:
            load_history(path)
        assert error.value.path == path, "the typed error must retain its source path"
        assert reason in error.value.reason, "the typed error must retain its reason"

    def test_a_malformed_sample_is_an_error_not_a_silent_drop(self) -> None:
        """A recognized schema with a broken sample is not read as empty."""
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

    def test_a_missing_history_has_a_distinct_typed_error(
        self, tmp_path: pth.Path
    ) -> None:
        """An absent optional artefact has a distinct error."""
        path = tmp_path / "main-baseline-history.json"
        with pytest.raises(BaselineHistoryNotFoundError) as error:
            load_history(path)
        assert error.value.path == path, "the not-found error must retain its path"
        assert error.value.reason == "does not exist", (
            "the not-found error must expose its stable reason"
        )

    def test_a_failed_history_replacement_preserves_the_previous_file(
        self, tmp_path: pth.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Failed publication cannot truncate the last complete history."""
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

    def test_a_failed_history_sync_removes_the_temporary_file(
        self, tmp_path: pth.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A write-stage failure leaves no temporary history file."""
        path = tmp_path / "main-baseline-history.json"

        def _sync_fails(_: int) -> None:
            """Simulate storage refusing to persist the temporary payload."""
            message = "simulated fsync failure"
            raise OSError(message)

        monkeypatch.setattr("benchmarks.ratchet_history.os.fsync", _sync_fails)
        with pytest.raises(OSError, match="simulated fsync failure"):
            write_history(history=_history(2.0), output_path=path)
        assert not list(tmp_path.glob(".main-baseline-history.json.*")), (
            "a failed temporary-file sync must remove the unpublished history file"
        )
