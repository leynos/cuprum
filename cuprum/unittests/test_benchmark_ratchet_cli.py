"""Unit tests for the benchmark-ratchet command-line interface."""

from __future__ import annotations

import json
import sys
import typing as typ

import pytest

from benchmarks.benchmark_profile import BENCHMARK_PROFILE_VERSION
from benchmarks.ratchet_history import BaselineHistory, HistorySample, write_history
from benchmarks.ratchet_rust_performance import main

if typ.TYPE_CHECKING:
    import pathlib as pth

SCENARIO = "medium-single-nocb"
WORKER_ITERATIONS = 20


def _write_json(
    *,
    tmp_path: pth.Path,
    filename: str,
    payload: dict[str, object],
) -> pth.Path:
    """Write one ratchet CLI input file."""
    path = tmp_path / filename
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _candidate_plan() -> dict[str, object]:
    """Return a candidate plan with one matching Rust/Python scenario pair."""
    return {
        "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
        "worker_iterations": WORKER_ITERATIONS,
        "scenarios": [
            {"name": f"python-{SCENARIO}", "backend": "python"},
            {"name": f"rust-{SCENARIO}", "backend": "rust"},
        ],
    }


def _candidate_throughput() -> dict[str, object]:
    """Return matching one-second Python and Rust measurements."""
    return {
        "results": [
            {"command": f"python-{SCENARIO}", "mean": 1.0},
            {"command": f"rust-{SCENARIO}", "mean": 1.0},
        ],
    }


def test_history_only_artefact_does_not_require_fallback_files(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A compatible history must remain usable after a failed main benchmark."""
    candidate_plan = _write_json(
        tmp_path=tmp_path,
        filename="candidate-plan.json",
        payload=_candidate_plan(),
    )
    candidate_throughput = _write_json(
        tmp_path=tmp_path,
        filename="candidate-throughput.json",
        payload=_candidate_throughput(),
    )
    history = tmp_path / "main-baseline-history.json"
    write_history(
        history=BaselineHistory(
            samples=(
                HistorySample(
                    commit="0" * 40,
                    run_id="1",
                    benchmark_profile_version=BENCHMARK_PROFILE_VERSION,
                    worker_iterations=WORKER_ITERATIONS,
                    ratios={SCENARIO: 1.0},
                ),
            )
        ),
        output_path=history,
    )
    output = tmp_path / "ratchet-report.json"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ratchet_rust_performance.py",
            "--baseline-plan",
            str(tmp_path / "missing-main-plan.json"),
            "--baseline-throughput",
            str(tmp_path / "missing-main-throughput.json"),
            "--baseline-history",
            str(history),
            "--candidate-plan",
            str(candidate_plan),
            "--candidate-throughput",
            str(candidate_throughput),
            "--output",
            str(output),
        ],
    )

    assert main() == 0, "a compatible history must not require fallback files"
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["baseline_sample_count"] == 1, (
        "the compatible history must supply one baseline sample; observed "
        f"{report['baseline_sample_count']!r}"
    )


def test_a_directory_baseline_history_returns_an_input_error(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an absent history falls back; a directory is an invalid input."""
    candidate_plan = _write_json(
        tmp_path=tmp_path,
        filename="candidate-plan.json",
        payload=_candidate_plan(),
    )
    candidate_throughput = _write_json(
        tmp_path=tmp_path,
        filename="candidate-throughput.json",
        payload=_candidate_throughput(),
    )
    history = tmp_path / "main-baseline-history"
    history.mkdir()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ratchet_rust_performance.py",
            "--baseline-history",
            str(history),
            "--candidate-plan",
            str(candidate_plan),
            "--candidate-throughput",
            str(candidate_throughput),
            "--output",
            str(tmp_path / "ratchet-report.json"),
        ],
    )

    assert main() == 2, "a directory baseline history must return input error 2"


def test_cli_applies_non_default_history_policy_options(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI must serialize policy values supplied through history options."""
    candidate_plan = _write_json(
        tmp_path=tmp_path,
        filename="candidate-plan.json",
        payload=_candidate_plan(),
    )
    candidate_throughput = _write_json(
        tmp_path=tmp_path,
        filename="candidate-throughput.json",
        payload=_candidate_throughput(),
    )
    history = tmp_path / "main-baseline-history.json"
    write_history(
        history=BaselineHistory(
            samples=tuple(
                HistorySample(
                    commit="0" * 40,
                    run_id=str(index),
                    benchmark_profile_version=BENCHMARK_PROFILE_VERSION,
                    worker_iterations=WORKER_ITERATIONS,
                    ratios={SCENARIO: ratio},
                )
                for index, ratio in enumerate((1.0, 1.2, 0.8, 1.0))
            )
        ),
        output_path=history,
    )
    output = tmp_path / "ratchet-report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ratchet_rust_performance.py",
            "--baseline-history",
            str(history),
            "--candidate-plan",
            str(candidate_plan),
            "--candidate-throughput",
            str(candidate_throughput),
            "--history-window",
            "3",
            "--noise-sigmas",
            "2.0",
            "--max-regression",
            "0.4",
            "--output",
            str(output),
        ],
    )

    assert main() == 0, "the candidate at the median must pass the configured policy"
    comparison = json.loads(output.read_text(encoding="utf-8"))["comparisons"][0]
    assert comparison["baseline_sample_count"] == 3, (
        "--history-window must retain exactly three recent samples; observed "
        f"{comparison['baseline_sample_count']!r}"
    )
    assert comparison["max_regression"] == pytest.approx(0.4), (
        "--max-regression must be serialized in the comparison report"
    )
    assert comparison["noise_tolerance"] == pytest.approx(0.59304), (
        "--noise-sigmas must set the MAD-derived noise band"
    )
    assert comparison["effective_threshold"] == pytest.approx(0.59304), (
        "the wider configured noise band must determine the effective threshold"
    )
