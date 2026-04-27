"""Tests for the tee profiling benchmark harness."""

from __future__ import annotations

import json
import sys
import typing as typ

from benchmarks import profile_tee_hotpath
from benchmarks.deterministic_b64_fixture import FixtureConfig, write_fixture
from benchmarks.profile_tee_hotpath import (
    TeeProfileDriverConfig,
    default_tee_profile_scenarios,
    run_profile_plan,
)
from benchmarks.summarize_folded import summarize_folded_file
from benchmarks.tee_profile_worker import TeeProfileWorkerConfig, run_tee_profile_worker

if typ.TYPE_CHECKING:
    import pathlib as pth

    import pytest


def test_fixture_generation_is_repeatable(tmp_path: pth.Path) -> None:
    """The same seed and size produce identical manifest hashes."""
    first_output = tmp_path / "first.b64"
    first_manifest = tmp_path / "first.json"
    second_output = tmp_path / "second.b64"
    second_manifest = tmp_path / "second.json"

    config = FixtureConfig(seed=12345, raw_bytes=4096, wrap=76)
    first = write_fixture(config, output=first_output, manifest=first_manifest)
    second = write_fixture(config, output=second_output, manifest=second_manifest)

    assert first["sha256"] == second["sha256"], (
        f"expected repeat fixture hashes to match, got {first['sha256']} "
        f"and {second['sha256']}"
    )
    assert first_output.read_bytes() == second_output.read_bytes(), (
        f"expected fixture bytes in {first_output} and {second_output} to match"
    )
    assert json.loads(first_manifest.read_text())["sha256"] == first["sha256"], (
        f"expected manifest {first_manifest} to record hash {first['sha256']}"
    )


def test_folded_summary_empty_file_yields_zero_totals(tmp_path: pth.Path) -> None:
    """Empty folded input produces zero samples and empty rankings."""
    folded = tmp_path / "stacks.folded"
    folded.write_text("")
    summary_path = tmp_path / "summary.json"

    summary = summarize_folded_file(
        folded,
        output=summary_path,
        limit=5,
        example_limit=2,
    )
    top_leaf = typ.cast("list[dict[str, object]]", summary["top_leaf_frames"])
    top_inclusive = typ.cast(
        "list[dict[str, object]]",
        summary["top_inclusive_frames"],
    )

    assert summary["total_samples"] == 0, (
        f"expected empty folded input to have 0 samples, got {summary['total_samples']}"
    )
    assert top_leaf == [], f"expected no leaf rankings, got {top_leaf}"
    assert top_inclusive == [], f"expected no inclusive rankings, got {top_inclusive}"
    assert summary_path.exists(), f"expected summary file at {summary_path}"


def test_folded_summary_all_invalid_lines_yield_zero_totals(
    tmp_path: pth.Path,
) -> None:
    """Malformed folded lines are ignored and do not contribute samples."""
    folded = tmp_path / "stacks.folded"
    folded.write_text("root;leaf\nroot;leaf not_an_int\n; 3\n")
    summary_path = tmp_path / "summary.json"

    summary = summarize_folded_file(
        folded,
        output=summary_path,
        limit=5,
        example_limit=2,
    )
    top_leaf = typ.cast("list[dict[str, object]]", summary["top_leaf_frames"])
    top_inclusive = typ.cast(
        "list[dict[str, object]]",
        summary["top_inclusive_frames"],
    )

    assert summary["total_samples"] == 0, (
        "expected invalid folded input to have 0 samples, "
        f"got {summary['total_samples']}"
    )
    assert top_leaf == [], f"expected no leaf rankings, got {top_leaf}"
    assert top_inclusive == [], f"expected no inclusive rankings, got {top_inclusive}"
    assert summary_path.exists(), f"expected summary file at {summary_path}"


def test_folded_summary_ranks_inclusive_and_leaf_frames(tmp_path: pth.Path) -> None:
    """Folded stack summaries expose ranked frame costs."""
    folded = tmp_path / "stacks.folded"
    folded.write_text("root;parent;leaf 3\nroot;other 2\nroot;parent;leaf 1\n")
    summary_path = tmp_path / "summary.json"

    summary = summarize_folded_file(
        folded,
        output=summary_path,
        limit=5,
        example_limit=2,
    )
    top_leaf = typ.cast("list[dict[str, object]]", summary["top_leaf_frames"])
    top_inclusive = typ.cast(
        "list[dict[str, object]]",
        summary["top_inclusive_frames"],
    )

    assert summary["total_samples"] == 6, (
        f"expected 6 folded samples, got {summary['total_samples']}"
    )
    assert top_leaf[0]["frame"] == "leaf", (
        f"expected top leaf frame to be leaf, got {top_leaf[0]}"
    )
    assert top_leaf[0]["leaf_samples"] == 4, (
        f"expected leaf frame to have 4 leaf samples, got {top_leaf[0]}"
    )
    assert top_inclusive[0]["frame"] == "root", (
        f"expected top inclusive frame to be root, got {top_inclusive[0]}"
    )
    assert summary_path.exists(), f"expected summary file at {summary_path}"


def test_profile_plan_contains_initial_matrix(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default plan preserves the required scenario matrix."""
    monkeypatch.setattr(profile_tee_hotpath, "can_use_rust_backend", lambda: True)
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJj\n")
    wrapped = tmp_path / "fixture-wrap76.b64"
    wrapped.write_text("YWJj\n")
    config = TeeProfileDriverConfig(
        fixture_path=fixture,
        wrapped_fixture_path=wrapped,
        output_dir=tmp_path / "profiles",
        profiler="none",
        warmup_count=1,
        repeat_count=3,
    )

    plan = run_profile_plan(config=config)
    scenarios = typ.cast("list[dict[str, object]]", plan["scenarios"])

    assert [scenario["name"] for scenario in scenarios] == [
        "echo-devnull-nocb-s1",
        "echo-textblackhole-nocb-s1",
        "echo-pty-nocb-s1",
        "tee-devnull-nocb-s1",
        "echo-devnull-cb-s1",
        "echo-devnull-nocb-s4-python",
        "echo-devnull-nocb-s4-rust",
    ], f"expected full scenario matrix when Rust is available, got {scenarios}"


def test_profile_plan_omits_rust_backend_when_unavailable(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default plan excludes Rust scenarios when Rust is unavailable."""
    monkeypatch.setattr(profile_tee_hotpath, "can_use_rust_backend", lambda: False)
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJj\n")
    wrapped = tmp_path / "fixture-wrap76.b64"
    wrapped.write_text("YWJj\n")
    config = TeeProfileDriverConfig(
        fixture_path=fixture,
        wrapped_fixture_path=wrapped,
        output_dir=tmp_path / "profiles",
        profiler="none",
        warmup_count=1,
        repeat_count=3,
    )

    plan = run_profile_plan(config=config)
    scenarios = typ.cast("list[dict[str, object]]", plan["scenarios"])
    scenario_names = [scenario["name"] for scenario in scenarios]

    assert scenario_names == [
        "echo-devnull-nocb-s1",
        "echo-textblackhole-nocb-s1",
        "echo-pty-nocb-s1",
        "tee-devnull-nocb-s1",
        "echo-devnull-cb-s1",
        "echo-devnull-nocb-s4-python",
    ], f"expected Rust scenario to be omitted, got {scenario_names}"


def test_worker_exercises_parent_side_consume_path(tmp_path: pth.Path) -> None:
    """A small fixture can run through echo, capture, and tee modes."""
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJjZGVm\n")

    for mode in ("echo", "capture", "tee"):
        result = run_tee_profile_worker(
            TeeProfileWorkerConfig(
                fixture_path=fixture,
                stages=1,
                mode=mode,
                sink_kind="devnull",
                with_line_callbacks=False,
                backend="python",
                repeat_count=1,
            ),
        )

        assert result["status"] == "ok", (
            f"expected worker status ok for {mode}, got {result}"
        )
        assert result["exit_code"] == 0, (
            f"expected worker exit code 0 for {mode}, got {result}"
        )
        assert result["scenario"] == f"{mode}-devnull-nocb-s1-python", (
            f"expected scenario label for {mode}, got {result['scenario']}"
        )
        captured_output_length = typ.cast("int", result["captured_output_length"])
        if mode == "echo":
            assert captured_output_length == 0, (
                "expected echo-only mode to capture 0 bytes, "
                f"got {captured_output_length}"
            )
        else:
            assert captured_output_length > 0, (
                f"expected {mode} mode to capture bytes, got {captured_output_length}"
            )
        assert result["stdout_line_count"] == 0, (
            f"expected no line callbacks for {mode}, got {result['stdout_line_count']}"
        )


def test_worker_exercises_parent_side_consume_path_with_callbacks(
    tmp_path: pth.Path,
) -> None:
    """A small fixture can run through all modes with line callbacks."""
    fixture = tmp_path / "fixture_with_cb.b64"
    fixture.write_text("YWJjZGVm\n")

    for mode in ("echo", "capture", "tee"):
        result = run_tee_profile_worker(
            TeeProfileWorkerConfig(
                fixture_path=fixture,
                stages=1,
                mode=mode,
                sink_kind="devnull",
                with_line_callbacks=True,
                backend="python",
                repeat_count=1,
            ),
        )

        assert result["status"] == "ok", (
            f"expected worker status ok for callback {mode}, got {result}"
        )
        assert result["exit_code"] == 0, (
            f"expected worker exit code 0 for callback {mode}, got {result}"
        )
        assert result["scenario"] == f"{mode}-devnull-cb-s1-python", (
            f"expected callback scenario label for {mode}, got {result['scenario']}"
        )
        captured_output_length = typ.cast("int", result["captured_output_length"])
        if mode == "echo":
            assert captured_output_length == 0, (
                f"expected echo-only callback mode to capture 0 bytes, "
                f"got {captured_output_length}"
            )
        else:
            assert captured_output_length > 0, (
                f"expected callback {mode} mode to capture bytes, "
                f"got {captured_output_length}"
            )
        stdout_line_count = typ.cast("int", result["stdout_line_count"])
        assert stdout_line_count > 0, (
            f"expected callback {mode} mode to observe lines, got {stdout_line_count}"
        )


def test_worker_accumulates_repeat_counters(tmp_path: pth.Path) -> None:
    """Worker output counters accumulate over repeated measured runs."""
    fixture = tmp_path / "fixture_repeat.b64"
    fixture.write_text("YWJjZGVm\n")

    result = run_tee_profile_worker(
        TeeProfileWorkerConfig(
            fixture_path=fixture,
            stages=1,
            mode="tee",
            sink_kind="devnull",
            with_line_callbacks=True,
            backend="python",
            repeat_count=3,
        ),
    )

    assert result["status"] == "ok", f"expected worker status ok, got {result}"
    assert result["exit_code"] == 0, f"expected worker exit code 0, got {result}"
    assert result["captured_output_length"] == len(fixture.read_text()) * 3, (
        "expected captured output length to accumulate across repeats, "
        f"got {result['captured_output_length']}"
    )
    assert result["stdout_line_count"] == 3, (
        f"expected line callbacks to accumulate across repeats, got {result}"
    )


def test_default_scenarios_use_requested_repeat_count(tmp_path: pth.Path) -> None:
    """Scenario expansion keeps fixed repeat counts across the matrix."""
    fixture = tmp_path / "fixture.b64"
    wrapped = tmp_path / "fixture-wrap76.b64"

    scenarios = default_tee_profile_scenarios(
        fixture_path=fixture,
        wrapped_fixture_path=wrapped,
        repeat_count=7,
    )

    assert {scenario.repeat_count for scenario in scenarios} == {7}, (
        f"expected every scenario repeat count to be 7, got {scenarios}"
    )


def test_profile_cli_returns_scenario_worker_failure_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run-scenario returns the worker exit code when the worker fails."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "profile_tee_hotpath.py",
            "run-scenario",
            "--scenario",
            "echo-devnull-nocb-s1",
        ],
    )
    monkeypatch.setattr(
        profile_tee_hotpath,
        "run_profile_scenario",
        lambda *, config: {"status": "failed", "exit_code": 17},
    )

    assert profile_tee_hotpath.main() == 17, (
        "expected run-scenario CLI to return worker failure exit code 17"
    )


def test_profile_cli_returns_matrix_failure_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run returns non-zero when any matrix scenario reports failure."""
    monkeypatch.setattr(sys, "argv", ["profile_tee_hotpath.py", "run"])
    monkeypatch.setattr(
        profile_tee_hotpath,
        "run_profile_matrix",
        lambda *, config: [
            {"status": "ok", "exit_code": 0},
            {"status": "failed", "exit_code": 3},
        ],
    )

    assert profile_tee_hotpath.main() == 3, (
        "expected matrix CLI to return first worker failure exit code 3"
    )
