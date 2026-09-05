"""Unit tests for the benchmark-ratchet command-line interface."""

from __future__ import annotations

import json
import sys
import typing as typ

import pytest

from benchmarks.benchmark_profile import BENCHMARK_PROFILE_VERSION
from benchmarks.ratchet_history import BaselineHistory, HistorySample, write_history
from benchmarks.ratchet_rust_performance import main
from cuprum.unittests.conftest import SCENARIO, WORKER_ITERATIONS

if typ.TYPE_CHECKING:
    import pathlib as pth


def _write_history_window(*, path: pth.Path, ratios: tuple[float, ...]) -> None:
    """Write compatible history samples with the specified ratios."""
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
                for index, ratio in enumerate(ratios)
            )
        ),
        output_path=path,
    )


def test_history_only_artefact_does_not_require_fallback_files(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_artefacts: tuple[pth.Path, pth.Path],
) -> None:
    """A compatible history must remain usable after a failed main benchmark."""
    candidate_plan, candidate_throughput = candidate_artefacts
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
    assert report["baseline_source"] == "history", (
        "a compatible history-only artefact must be recorded as the baseline source"
    )
    assert report["baseline_reason"] == "compatible_history", (
        "the report must retain why its history window was selected"
    )
    assert report["compatible_sample_count"] == 1, (
        "the report must retain the compatible history evidence count"
    )
    assert report["comparison_state"] == "compared", (
        "a history-backed report must record that it compared the candidate"
    )


def test_a_directory_baseline_history_returns_an_input_error(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_artefacts: tuple[pth.Path, pth.Path],
) -> None:
    """Only an absent history falls back; a directory is an invalid input."""
    candidate_plan, candidate_throughput = candidate_artefacts
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
    candidate_artefacts: tuple[pth.Path, pth.Path],
) -> None:
    """The CLI must serialize policy values supplied through history options."""
    candidate_plan, candidate_throughput = candidate_artefacts
    history = tmp_path / "main-baseline-history.json"
    _write_history_window(path=history, ratios=(1.0, 1.2, 0.8, 1.0))
    output = tmp_path / "ratchet-report.json"
    exit_code = main([
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
    ])

    assert exit_code == 0, "the candidate at the median must pass the configured policy"
    report = json.loads(output.read_text(encoding="utf-8"))
    comparison = report["comparisons"][0]
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
    assert report["baseline_source"] == "history", (
        "multiple compatible samples must select the history baseline"
    )
    assert report["baseline_reason"] == "compatible_history", (
        "the report must retain why the history window was selected"
    )
    assert report["compatible_sample_count"] == 3, (
        "the decision record must retain compatible samples after applying the window"
    )
    assert report["comparison_state"] == "compared", (
        "a compatible history window must compare the candidate"
    )


def test_cli_records_fallback_when_history_is_unavailable(
    tmp_path: pth.Path,
    candidate_artefacts: tuple[pth.Path, pth.Path],
) -> None:
    """The legacy baseline must record why it replaced unavailable history."""
    candidate_plan, candidate_throughput = candidate_artefacts
    output = tmp_path / "ratchet-report.json"

    exit_code = main([
        "--baseline-history",
        str(tmp_path / "absent-history.json"),
        "--baseline-plan",
        str(candidate_plan),
        "--baseline-throughput",
        str(candidate_throughput),
        "--candidate-plan",
        str(candidate_plan),
        "--candidate-throughput",
        str(candidate_throughput),
        "--output",
        str(output),
    ])
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0, "a matching fallback baseline must pass"
    assert report["baseline_source"] == "fallback", (
        "an unavailable history must select the legacy fallback source"
    )
    assert report["baseline_reason"] == "history_unavailable", (
        "the fallback report must retain the absent-history reason"
    )
    assert report["compatible_sample_count"] == 0, (
        "an unavailable history cannot contribute compatible samples"
    )
    assert report["comparison_state"] == "compared", (
        "a fallback baseline must still compare the candidate"
    )


def test_cli_records_fallback_when_history_has_no_compatible_samples(
    tmp_path: pth.Path,
    candidate_artefacts: tuple[pth.Path, pth.Path],
) -> None:
    """A profile-pruned history must explain why the fallback was selected."""
    candidate_plan, candidate_throughput = candidate_artefacts
    history = tmp_path / "main-baseline-history.json"
    write_history(
        history=BaselineHistory(
            samples=(
                HistorySample(
                    commit="0" * 40,
                    run_id="1",
                    benchmark_profile_version="obsolete-profile",
                    worker_iterations=WORKER_ITERATIONS,
                    ratios={SCENARIO: 1.0},
                ),
            )
        ),
        output_path=history,
    )
    output = tmp_path / "ratchet-report.json"

    exit_code = main([
        "--baseline-history",
        str(history),
        "--baseline-plan",
        str(candidate_plan),
        "--baseline-throughput",
        str(candidate_throughput),
        "--candidate-plan",
        str(candidate_plan),
        "--candidate-throughput",
        str(candidate_throughput),
        "--output",
        str(output),
    ])
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0, "the matching fallback must pass after profile pruning"
    assert report["baseline_source"] == "fallback", (
        "a profile-pruned history must select the legacy fallback"
    )
    assert report["baseline_reason"] == "no_compatible_history", (
        "the fallback report must retain that no history sample was compatible"
    )
    assert report["compatible_sample_count"] == 0, (
        "profile-pruned history must report zero compatible samples"
    )
    assert report["comparison_state"] == "compared", (
        "a fallback after history pruning must still compare"
    )


def test_cli_skips_without_any_baseline(
    tmp_path: pth.Path,
    candidate_artefacts: tuple[pth.Path, pth.Path],
) -> None:
    """A first run must persist a typed no-baseline skip report."""
    candidate_plan, candidate_throughput = candidate_artefacts
    output = tmp_path / "ratchet-report.json"

    exit_code = main([
        "--candidate-plan",
        str(candidate_plan),
        "--candidate-throughput",
        str(candidate_throughput),
        "--output",
        str(output),
    ])
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0, "a first run without a baseline must intentionally pass"
    assert report["baseline_source"] == "none", (
        "a skipped first run must select no baseline source"
    )
    assert report["baseline_reason"] == "no_baseline_available", (
        "a skipped first run must retain its bounded reason"
    )
    assert report["compatible_sample_count"] == 0, (
        "a skipped first run cannot have compatible history evidence"
    )
    assert report["comparison_state"] == "skipped_no_baseline", (
        "a skipped first run must not claim that it compared the candidate"
    )


def test_cli_skips_an_incompatible_fallback_profile(
    tmp_path: pth.Path,
    candidate_artefacts: tuple[pth.Path, pth.Path],
) -> None:
    """A mismatched fallback profile must persist an incompatible skip reason."""
    candidate_plan, candidate_throughput = candidate_artefacts
    fallback_plan = tmp_path / "legacy-plan.json"
    fallback_payload = json.loads(candidate_plan.read_text(encoding="utf-8"))
    fallback_payload["benchmark_profile_version"] = "legacy-profile"
    fallback_plan.write_text(json.dumps(fallback_payload), encoding="utf-8")
    output = tmp_path / "ratchet-report.json"

    exit_code = main([
        "--baseline-plan",
        str(fallback_plan),
        "--baseline-throughput",
        str(candidate_throughput),
        "--candidate-plan",
        str(candidate_plan),
        "--candidate-throughput",
        str(candidate_throughput),
        "--output",
        str(output),
    ])
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0, "an incompatible profile must intentionally skip"
    assert report["baseline_source"] == "none", (
        "an incompatible profile must not claim a selected baseline source"
    )
    assert report["baseline_reason"] == "incompatible_profile", (
        "an incompatible profile must have a bounded decision reason"
    )
    assert report["compatible_sample_count"] == 0, (
        "an incompatible fallback must not report compatible history samples"
    )
    assert report["comparison_state"] == "skipped_incompatible_profile", (
        "an incompatible profile must not claim that it compared"
    )


def test_cli_records_a_history_backed_regression(
    tmp_path: pth.Path,
    candidate_artefacts: tuple[pth.Path, pth.Path],
) -> None:
    """A comparison failure must retain the same durable decision evidence."""
    candidate_plan, candidate_throughput = candidate_artefacts
    history_path = tmp_path / "main-baseline-history.json"
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
        output_path=history_path,
    )
    regression_throughput = tmp_path / "regression-throughput.json"
    throughput_payload = json.loads(candidate_throughput.read_text(encoding="utf-8"))
    throughput_payload["results"][1]["mean"] = 1.5
    regression_throughput.write_text(json.dumps(throughput_payload), encoding="utf-8")
    output = tmp_path / "ratchet-report.json"

    exit_code = main([
        "--baseline-history",
        str(history_path),
        "--candidate-plan",
        str(candidate_plan),
        "--candidate-throughput",
        str(regression_throughput),
        "--output",
        str(output),
    ])
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 1, "a 50 percent history-backed regression must fail"
    assert report["passed"] is False, "the persisted report must retain the failure"
    assert report["baseline_source"] == "history", (
        "a history-backed regression must retain its baseline source"
    )
    assert report["comparison_state"] == "compared", (
        "a history-backed regression must record that comparison occurred"
    )
