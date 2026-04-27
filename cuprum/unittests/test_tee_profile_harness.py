"""Tests for the tee profiling benchmark harness."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - integration tests exercise fixed CLI commands.
import sys
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from benchmarks import profile_tee_hotpath, tee_profile_worker
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

    from syrupy.assertion import SnapshotAssertion


def _summarise_folded(
    tmp_path: pth.Path,
    content: str,
) -> tuple[
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
    pth.Path,
]:
    """Write a folded file, run summarize_folded_file, and return parsed results."""
    folded = tmp_path / "stacks.folded"
    folded.write_text(content)
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
    return summary, top_leaf, top_inclusive, summary_path


def _redact(obj: object, keys: frozenset[str]) -> object:
    """Recursively replace the values of nominated keys with '<redacted>'."""
    if isinstance(obj, dict):
        return {
            k: "<redacted>" if k in keys else _redact(v, keys) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(item, keys) for item in obj]
    return obj


_VOLATILE_KEYS = frozenset({
    "sha256",
    "wall_time_seconds",
    "output_bytes",
    "fixture_path",
    "wrapped_fixture_path",
    "output_dir",
    "profile_dir",
    "worker_command",
})

_SCENARIO_NAMES_WITH_RUST: list[str] = [
    "echo-devnull-nocb-s1",
    "echo-textblackhole-nocb-s1",
    "echo-pty-nocb-s1",
    "tee-devnull-nocb-s1",
    "echo-devnull-cb-s1",
    "echo-devnull-nocb-s4-python",
    "echo-devnull-nocb-s4-rust",
]

_SCENARIO_NAMES_WITHOUT_RUST: list[str] = [
    "echo-devnull-nocb-s1",
    "echo-textblackhole-nocb-s1",
    "echo-pty-nocb-s1",
    "tee-devnull-nocb-s1",
    "echo-devnull-cb-s1",
    "echo-devnull-nocb-s4-python",
]


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


def test_write_fixture_manifest_snapshot(
    tmp_path: pth.Path,
    snapshot: SnapshotAssertion,
) -> None:
    """write_fixture manifest structure matches snapshot."""
    config = FixtureConfig(seed=42, raw_bytes=96, wrap=0)
    result = write_fixture(
        config,
        output=tmp_path / "fixture.b64",
        manifest=tmp_path / "manifest.json",
    )
    assert _redact(result, _VOLATILE_KEYS) == snapshot


def test_folded_summary_empty_file_yields_zero_totals(tmp_path: pth.Path) -> None:
    """Empty folded input produces zero samples and empty rankings."""
    summary, top_leaf, top_inclusive, summary_path = _summarise_folded(tmp_path, "")
    assert summary["total_samples"] == 0
    assert top_leaf == []
    assert top_inclusive == []
    assert summary_path.exists()


def test_folded_summary_all_invalid_lines_yield_zero_totals(
    tmp_path: pth.Path,
) -> None:
    """Malformed folded lines are ignored and do not contribute samples."""
    summary, top_leaf, top_inclusive, summary_path = _summarise_folded(
        tmp_path, "root;leaf\nroot;leaf not_an_int\n; 3\n"
    )
    assert summary["total_samples"] == 0
    assert top_leaf == []
    assert top_inclusive == []
    assert summary_path.exists()


def test_folded_summary_ranks_inclusive_and_leaf_frames(tmp_path: pth.Path) -> None:
    """Folded stack summaries expose ranked frame costs."""
    summary, top_leaf, top_inclusive, summary_path = _summarise_folded(
        tmp_path, "root;parent;leaf 3\nroot;other 2\nroot;parent;leaf 1\n"
    )
    assert summary["total_samples"] == 6
    assert top_leaf[0]["frame"] == "leaf"
    assert top_leaf[0]["leaf_samples"] == 4
    assert top_inclusive[0]["frame"] == "root"
    assert summary_path.exists()


def test_summarize_folded_snapshot(
    tmp_path: pth.Path,
    snapshot: SnapshotAssertion,
) -> None:
    """summarize_folded_file output structure matches snapshot."""
    summary, _, _, _ = _summarise_folded(
        tmp_path, "root;parent;leaf 3\nroot;other 2\nroot;parent;leaf 1\n"
    )
    assert _redact(summary, _VOLATILE_KEYS) == snapshot


def test_folded_summary_counts_repeated_frames_once_per_stack(
    tmp_path: pth.Path,
) -> None:
    """Inclusive folded counts deduplicate frames within one stack."""
    summary, _top_leaf, top_inclusive, _summary_path = _summarise_folded(
        tmp_path, "root;recursive;recursive;leaf 3\n"
    )

    recursive = next(entry for entry in top_inclusive if entry["frame"] == "recursive")

    assert summary["total_samples"] == 3
    assert recursive["inclusive_samples"] == 3
    assert recursive["example_stacks"] == ["root;recursive;recursive;leaf"]


@pytest.mark.parametrize(
    ("rust_available", "expected_names"),
    [
        pytest.param(True, _SCENARIO_NAMES_WITH_RUST, id="rust-available"),
        pytest.param(False, _SCENARIO_NAMES_WITHOUT_RUST, id="rust-unavailable"),
    ],
)
def test_profile_plan_scenario_matrix(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
    rust_available: bool,  # noqa: FBT001 - pytest parametrises this value.
    expected_names: list[str],
) -> None:
    """The default plan includes or omits the Rust scenario.

    Backend availability controls whether the Rust scenario is present.
    """
    monkeypatch.setattr(
        profile_tee_hotpath, "can_use_rust_backend", lambda: rust_available
    )
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

    assert [scenario["name"] for scenario in scenarios] == expected_names, (
        f"expected scenario matrix for rust_available={rust_available!r}, "
        f"got {scenarios}"
    )


def test_run_profile_plan_snapshot(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot: SnapshotAssertion,
) -> None:
    """run_profile_plan output structure matches snapshot."""
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
        repeat_count=1,
    )
    plan = run_profile_plan(config=config)
    assert _redact(plan, _VOLATILE_KEYS) == snapshot


@pytest.mark.parametrize("with_line_callbacks", [False, True])
def test_worker_exercises_parent_side_consume_path(
    tmp_path: pth.Path,
    with_line_callbacks: bool,  # noqa: FBT001 - pytest parametrises this value.
) -> None:
    """A small fixture can run through echo, capture, and tee modes."""
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJjZGVm\n")
    cb_label = "cb" if with_line_callbacks else "nocb"

    for mode in ("echo", "capture", "tee"):
        result = run_tee_profile_worker(
            TeeProfileWorkerConfig(
                fixture_path=fixture,
                stages=1,
                mode=mode,
                sink_kind="devnull",
                with_line_callbacks=with_line_callbacks,
                backend="python",
                repeat_count=1,
            ),
        )

        assert result["status"] == "ok"
        assert result["exit_code"] == 0
        assert result["scenario"] == f"{mode}-devnull-{cb_label}-s1-python"
        captured_output_length = result["captured_output_length"]
        if mode == "echo":
            assert captured_output_length == 0
        else:
            assert captured_output_length > 0
        stdout_line_count = result["stdout_line_count"]
        if with_line_callbacks:
            assert stdout_line_count > 0
        else:
            assert stdout_line_count == 0


def test_run_tee_profile_worker_snapshot(
    tmp_path: pth.Path,
    snapshot: SnapshotAssertion,
) -> None:
    """run_tee_profile_worker output structure matches snapshot."""
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJjZGVm\n")

    result = run_tee_profile_worker(
        TeeProfileWorkerConfig(
            fixture_path=fixture,
            stages=1,
            mode="tee",
            sink_kind="devnull",
            with_line_callbacks=True,
            backend="python",
            repeat_count=1,
        ),
    )

    assert _redact(result, _VOLATILE_KEYS) == snapshot


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


def test_worker_cli_reports_config_errors(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Worker CLI returns a process-style error for invalid configuration."""
    missing_fixture = tmp_path / "missing.b64"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tee_profile_worker.py",
            "--fixture",
            str(missing_fixture),
            "--stages",
            "1",
            "--mode",
            "echo",
            "--sink-kind",
            "devnull",
        ],
    )

    assert tee_profile_worker.main() == 2
    captured = capsys.readouterr()
    assert f"fixture_path must exist and be a file: {missing_fixture}" in captured.err


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        pytest.param(
            {"stages": 0},
            "stages must be >= 1",
            id="stages-zero",
        ),
        pytest.param(
            {"repeat_count": 0},
            "repeat-count must be >= 1",
            id="repeat-count-zero",
        ),
        pytest.param(
            {"mode": "invalid"},
            "mode must be one of",
            id="invalid-mode",
        ),
        pytest.param(
            {"sink_kind": "invalid"},
            "sink-kind must be one of",
            id="invalid-sink",
        ),
        pytest.param(
            {"backend": "invalid"},
            "backend must be one of",
            id="invalid-backend",
        ),
    ],
)
def test_worker_config_rejects_invalid_fields(
    tmp_path: pth.Path,
    kwargs: dict[str, object],
    fragment: str,
) -> None:
    """TeeProfileWorkerConfig raises ValueError for invalid field values."""
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJj\n")
    base: dict[str, object] = {
        "fixture_path": fixture,
        "stages": 1,
        "mode": "echo",
        "sink_kind": "devnull",
        "with_line_callbacks": False,
        "backend": "python",
        "repeat_count": 1,
    }
    base.update(kwargs)
    config_type = typ.cast("typ.Any", TeeProfileWorkerConfig)
    with pytest.raises(ValueError, match=fragment):
        config_type(**base)


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        pytest.param(
            {"warmup_count": -1},
            "warmup-count must be >= 0",
            id="warmup-negative",
        ),
        pytest.param(
            {"repeat_count": 0},
            "repeat-count must be >= 1",
            id="repeat-zero",
        ),
        pytest.param(
            {"perf_frequency": 0},
            "perf-frequency must be >= 1",
            id="perf-freq-zero",
        ),
        pytest.param(
            {"perf_call_graph": "  "},
            "perf-call-graph must be a non-empty string",
            id="blank-call-graph",
        ),
    ],
)
def test_driver_config_rejects_invalid_fields(
    kwargs: dict[str, object],
    fragment: str,
) -> None:
    """TeeProfileDriverConfig raises ValueError for invalid numeric/string fields."""
    base: dict[str, object] = {
        "warmup_count": 1,
        "repeat_count": 3,
        "perf_frequency": 999,
        "perf_call_graph": "dwarf,16384",
    }
    base.update(kwargs)
    config_type = typ.cast("typ.Any", TeeProfileDriverConfig)
    with pytest.raises(ValueError, match=fragment):
        config_type(**base)


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        pytest.param(
            {"raw_bytes": -1},
            "raw-bytes must be >= 0",
            id="negative-raw-bytes",
        ),
        pytest.param(
            {"wrap": 1},
            "wrap must be 0 or 76",
            id="invalid-wrap",
        ),
    ],
)
def test_fixture_config_rejects_invalid_fields(
    kwargs: dict[str, object],
    fragment: str,
) -> None:
    """FixtureConfig raises ValueError for out-of-range fields."""
    base: dict[str, object] = {"seed": 0, "raw_bytes": 64, "wrap": 0}
    base.update(kwargs)
    config_type = typ.cast("typ.Any", FixtureConfig)
    with pytest.raises(ValueError, match=fragment):
        config_type(**base)


def test_worker_cli_runs_successfully_via_subprocess(tmp_path: pth.Path) -> None:
    """Worker CLI produces a valid JSON result when invoked as a subprocess."""
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJjZGVm\n")
    output = tmp_path / "result.json"
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "benchmarks.tee_profile_worker",
            "--fixture",
            str(fixture),
            "--stages",
            "1",
            "--mode",
            "echo",
            "--sink-kind",
            "devnull",
            "--backend",
            "python",
            "--repeat-count",
            "1",
            "--output",
            str(output),
        ],
        check=False,
    )

    assert completed.returncode == 0, f"expected exit 0, got {completed.returncode}"
    assert output.exists(), "expected worker-result.json to be written"
    result = json.loads(output.read_text())
    assert result["status"] == "ok"
    assert result["exit_code"] == 0


def test_worker_command_uses_module_invocation(tmp_path: pth.Path) -> None:
    """_worker_command produces a -m benchmarks.tee_profile_worker invocation."""
    from benchmarks.profile_tee_hotpath import TeeProfileScenario, _worker_command

    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJj\n")
    scenario = TeeProfileScenario(
        name="echo-devnull-nocb-s1",
        fixture_path=fixture,
        stages=1,
        mode="echo",
        sink_kind="devnull",
        with_line_callbacks=False,
        backend="python",
        repeat_count=1,
    )
    cmd = _worker_command(scenario)

    assert cmd[1] == "-m", f"expected -m flag at index 1, got {cmd}"
    assert cmd[2] == "benchmarks.tee_profile_worker", (
        f"expected module name at index 2, got {cmd}"
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


@given(
    seed=st.integers(min_value=0, max_value=2**31 - 1),
    raw_bytes=st.integers(min_value=0, max_value=4096),
    wrap=st.sampled_from([0, 76]),
)
@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_fixture_generation_is_deterministic(
    tmp_path: pth.Path,
    seed: int,
    raw_bytes: int,
    wrap: int,
) -> None:
    """write_fixture always produces the same SHA-256 for any valid FixtureConfig."""
    config = FixtureConfig(seed=seed, raw_bytes=raw_bytes, wrap=wrap)
    first = write_fixture(
        config,
        output=tmp_path / "a.b64",
        manifest=tmp_path / "a.json",
    )
    second = write_fixture(
        config,
        output=tmp_path / "b.b64",
        manifest=tmp_path / "b.json",
    )
    assert first["sha256"] == second["sha256"]
    assert first["output_bytes"] == second["output_bytes"]


@given(
    raw_bytes=st.integers(min_value=0, max_value=4096),
    wrap=st.sampled_from([0, 76]),
)
@settings(
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_fixture_output_bytes_matches_manifest(
    tmp_path: pth.Path,
    raw_bytes: int,
    wrap: int,
) -> None:
    """The output_bytes field in the manifest equals the fixture file size."""
    config = FixtureConfig(seed=0, raw_bytes=raw_bytes, wrap=wrap)
    output = tmp_path / "fixture.b64"
    manifest = tmp_path / "manifest.json"
    result = write_fixture(config, output=output, manifest=manifest)
    assert result["output_bytes"] == output.stat().st_size


@given(
    stacks=st.lists(
        st.tuples(
            st.lists(
                st.text(
                    alphabet=st.characters(
                        blacklist_categories=("Cs",),
                        blacklist_characters=(";", " ", "\n"),
                    ),
                    min_size=1,
                    max_size=20,
                ),
                min_size=1,
                max_size=5,
            ),
            st.integers(min_value=1, max_value=100),
        ),
        min_size=1,
        max_size=20,
    )
)
@settings(
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_folded_summary_total_samples_matches_input(
    tmp_path: pth.Path,
    stacks: list[tuple[list[str], int]],
) -> None:
    """total_samples always equals the sum of per-stack counts."""
    lines = "\n".join(f"{';'.join(frames)} {count}" for frames, count in stacks)
    summary, _leaf, _inclusive, _ = _summarise_folded(tmp_path, lines)
    expected_total = sum(count for _, count in stacks)
    assert summary["total_samples"] == expected_total
