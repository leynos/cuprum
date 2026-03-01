"""Unit tests for benchmark suite command and scenario helpers."""

from __future__ import annotations

import json
import pathlib as pth
import typing as typ

import pytest

from benchmarks._test_constants import _SCENARIO_NAME_PATTERN
from benchmarks.pipeline_throughput import (
    HyperfineConfig,
    PipelineBenchmarkConfig,
    PipelineBenchmarkScenario,
    build_hyperfine_command,
    default_pipeline_scenarios,
    render_prefixed_command,
    run_pipeline_benchmarks,
)


def test_default_pipeline_scenarios_include_python_backend() -> None:
    """The default matrix always includes a Python backend scenario."""
    scenarios = default_pipeline_scenarios(smoke=True, include_rust=True)

    assert any(s.backend == "python" for s in scenarios), (
        "expected python backend in default pipeline scenarios when "
        "smoke=True and include_rust=True"
    )


def test_default_pipeline_scenarios_gate_rust_backend() -> None:
    """Rust scenarios are omitted when ``include_rust`` is ``False``."""
    scenarios = default_pipeline_scenarios(smoke=True, include_rust=False)

    assert all(s.backend != "rust" for s in scenarios), (
        "expected rust backend to be omitted when include_rust=False"
    )


def test_render_prefixed_command_prefixes_env_and_quotes_tokens() -> None:
    """The rendered command prefixes env vars and safely quotes tokens."""
    command = ["uv", "run", "python", "benchmarks/pipeline_worker.py", "--label", "a b"]
    rendered = render_prefixed_command(
        command=command,
        env={"CUPRUM_STREAM_BACKEND": "python"},
    )

    assert rendered.startswith("CUPRUM_STREAM_BACKEND=python "), (
        "expected rendered command to include CUPRUM_STREAM_BACKEND prefix"
    )
    assert "'a b'" in rendered, "expected rendered command to quote spaced token"


def test_render_prefixed_command_with_empty_env_returns_raw_command() -> None:
    """When env is empty, rendered command should contain no env prefix."""
    command = ["uv", "run", "python", "benchmarks/pipeline_worker.py"]

    rendered = render_prefixed_command(command=command, env={})

    assert rendered == "uv run python benchmarks/pipeline_worker.py", (
        "expected rendered command to equal raw command when env is empty"
    )


def test_hyperfine_config_rejects_negative_warmup() -> None:
    """Warmup must be zero or positive."""
    with pytest.raises(ValueError, match="warmup must be >= 0"):
        HyperfineConfig(warmup=-1, runs=1)


def test_hyperfine_config_rejects_non_positive_runs() -> None:
    """Runs must be at least one."""
    with pytest.raises(ValueError, match="runs must be >= 1"):
        HyperfineConfig(warmup=0, runs=0)


def test_pipeline_benchmark_config_coerces_string_paths() -> None:
    """Config path fields accept strings and are normalized to ``Path``."""
    config = PipelineBenchmarkConfig(
        output_path="dist/benchmarks/bench.json",  # type: ignore[arg-type]
        worker_path="benchmarks/pipeline_worker.py",  # type: ignore[arg-type]
        scenarios=(),
        warmup=0,
        runs=1,
    )

    assert isinstance(config.output_path, pth.Path), (
        "expected output_path to be normalized to pathlib.Path"
    )
    assert isinstance(config.worker_path, pth.Path), (
        "expected worker_path to be normalized to pathlib.Path"
    )


def test_pipeline_benchmark_config_rejects_non_pathlike_output_path() -> None:
    """Non-path-like values for output_path raise a clear TypeError."""
    with pytest.raises(
        TypeError,
        match=r"output_path must be a pathlib\.Path or path-like value",
    ):
        PipelineBenchmarkConfig(
            output_path=123,  # type: ignore[arg-type]
            worker_path=pth.Path("benchmarks/pipeline_worker.py"),
            scenarios=(),
            warmup=0,
            runs=1,
        )


@pytest.mark.parametrize(
    ("kwargs", "error_match"),
    [
        pytest.param(
            {
                "name": "",
                "backend": "python",
                "payload_bytes": 1024,
                "stages": 2,
                "with_line_callbacks": False,
            },
            "name must be a non-empty string",
            id="empty_name",
        ),
        pytest.param(
            {
                "name": "pipeline-typo",
                "backend": "pythno",  # type: ignore[arg-type]
                "payload_bytes": 1024,
                "stages": 2,
                "with_line_callbacks": False,
            },
            "backend must be one of",
            id="invalid_backend",
        ),
        pytest.param(
            {
                "name": "pipeline-python",
                "backend": "python",
                "payload_bytes": 0,
                "stages": 2,
                "with_line_callbacks": False,
            },
            "payload_bytes must be > 0",
            id="non_positive_payload_bytes",
        ),
        pytest.param(
            {
                "name": "pipeline-python",
                "backend": "python",
                "payload_bytes": 1024,
                "stages": 1,
                "with_line_callbacks": False,
            },
            "stages must be >= 2",
            id="too_few_stages",
        ),
    ],
)
def test_pipeline_benchmark_scenario_validation(
    kwargs: dict[str, typ.Any],
    error_match: str,
) -> None:
    """PipelineBenchmarkScenario validates all critical parameters."""
    with pytest.raises(ValueError, match=error_match):
        PipelineBenchmarkScenario(**kwargs)


def test_build_hyperfine_command_requires_at_least_one_scenario(
    tmp_path: pth.Path,
) -> None:
    """At least one benchmark scenario is required."""
    config = PipelineBenchmarkConfig(
        output_path=tmp_path / "bench.json",
        worker_path=pth.Path("benchmarks/pipeline_worker.py"),
        scenarios=(),
        warmup=1,
        runs=2,
    )

    with pytest.raises(ValueError, match="at least one benchmark scenario is required"):
        build_hyperfine_command(config=config)


def test_build_hyperfine_command_contains_export_runs_and_warmup(
    tmp_path: pth.Path,
) -> None:
    """The hyperfine invocation includes required options and commands."""
    scenarios = (
        PipelineBenchmarkScenario(
            name="pipeline-python",
            backend="python",
            payload_bytes=1024,
            stages=2,
            with_line_callbacks=False,
        ),
    )
    config = PipelineBenchmarkConfig(
        output_path=tmp_path / "bench.json",
        worker_path=pth.Path("benchmarks/pipeline_worker.py"),
        scenarios=scenarios,
        warmup=1,
        runs=2,
    )
    command = build_hyperfine_command(config=config)

    assert command[0].endswith("hyperfine"), (
        "expected hyperfine executable to be the command prefix"
    )
    assert "--export-json" in command, "expected hyperfine command to export JSON"
    assert "--warmup" in command, "expected hyperfine command to include warmup"
    assert "--runs" in command, "expected hyperfine command to include runs"
    assert any("CUPRUM_STREAM_BACKEND=python" in token for token in command), (
        "expected at least one hyperfine scenario command to target python backend"
    )


def test_run_pipeline_benchmarks_dry_run_writes_json(tmp_path: pth.Path) -> None:
    """Dry-run mode writes plan JSON without invoking hyperfine."""
    output_path = tmp_path / "bench.json"
    scenarios = (
        PipelineBenchmarkScenario(
            name="pipeline-python",
            backend="python",
            payload_bytes=1024,
            stages=2,
            with_line_callbacks=False,
        ),
    )
    config = PipelineBenchmarkConfig(
        output_path=output_path,
        worker_path=pth.Path("benchmarks/pipeline_worker.py"),
        scenarios=scenarios,
        dry_run=True,
        warmup=1,
        runs=2,
    )
    result = run_pipeline_benchmarks(config=config)

    assert result.dry_run is True, "expected run result to indicate dry-run mode"
    assert output_path.is_file(), "expected dry-run output JSON file to be written"
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert payload["dry_run"] is True, "expected payload dry_run flag to be True"
    assert "rust_available" in payload, (
        "expected payload to include rust_available metadata"
    )
    assert isinstance(payload["rust_available"], bool), (
        "expected payload rust_available to be a boolean"
    )

    scenarios_payload = payload["scenarios"]
    assert isinstance(scenarios_payload, list), (
        "expected payload scenarios to be a list"
    )
    assert scenarios_payload, "expected payload scenarios list to be non-empty"
    assert any(
        isinstance(scenario, dict)
        and scenario.get("backend") == "python"
        and scenario.get("name") == "pipeline-python"
        for scenario in scenarios_payload
    ), "expected payload scenarios to include named python scenario"

    command_payload = payload["command"]
    assert isinstance(command_payload, list), "expected payload command to be a list"
    assert command_payload, "expected payload command list to be non-empty"
    assert all(isinstance(argument, str) for argument in command_payload), (
        "expected all payload command entries to be strings"
    )


# -- Scenario matrix tests (4.4.2) -------------------------------------------


def test_default_pipeline_scenarios_smoke_matrix_count() -> None:
    """Smoke mode with Python-only produces exactly 12 scenarios."""
    scenarios = default_pipeline_scenarios(smoke=True, include_rust=False)

    assert len(scenarios) == 12, (
        f"expected 12 smoke scenarios (3 sizes x 2 depths x 2 callbacks), "
        f"got {len(scenarios)}"
    )


def test_default_pipeline_scenarios_full_matrix_count() -> None:
    """Full mode with both backends produces exactly 24 scenarios."""
    scenarios = default_pipeline_scenarios(smoke=False, include_rust=True)

    assert len(scenarios) == 24, (
        f"expected 24 full scenarios (12 per backend x 2 backends), "
        f"got {len(scenarios)}"
    )


def test_default_pipeline_scenarios_names_are_systematic() -> None:
    """Every scenario name follows the systematic naming convention."""
    scenarios = default_pipeline_scenarios(smoke=True, include_rust=True)

    for scenario in scenarios:
        assert _SCENARIO_NAME_PATTERN.match(scenario.name), (
            f"scenario name {scenario.name!r} does not match expected pattern "
            f"{_SCENARIO_NAME_PATTERN.pattern!r}"
        )


def test_default_pipeline_scenarios_covers_all_payload_sizes() -> None:
    """The non-smoke matrix covers small (1 KB), medium (1 MB), large (100 MB)."""
    scenarios = default_pipeline_scenarios(smoke=False, include_rust=False)
    payload_sizes = {s.payload_bytes for s in scenarios}

    assert payload_sizes == {1024, 1_048_576, 104_857_600}, (
        f"expected payload sizes {{1024, 1048576, 104857600}}, got {payload_sizes}"
    )


def test_default_pipeline_scenarios_covers_both_depths() -> None:
    """The matrix covers both single-stage (2) and multi-stage (3) depths."""
    scenarios = default_pipeline_scenarios(smoke=True, include_rust=False)
    stage_counts = {s.stages for s in scenarios}

    assert stage_counts == {2, 3}, f"expected stage counts {{2, 3}}, got {stage_counts}"


def test_default_pipeline_scenarios_covers_both_callback_modes() -> None:
    """The matrix covers both with and without line callbacks."""
    scenarios = default_pipeline_scenarios(smoke=True, include_rust=False)
    callback_modes = {s.with_line_callbacks for s in scenarios}

    assert callback_modes == {True, False}, (
        f"expected callback modes {{True, False}}, got {callback_modes}"
    )


def test_default_pipeline_scenarios_smoke_uses_reduced_payloads() -> None:
    """Smoke mode uses reduced payload sizes (1 KB, 64 KB, 1 MB)."""
    scenarios = default_pipeline_scenarios(smoke=True, include_rust=False)
    payload_sizes = {s.payload_bytes for s in scenarios}

    assert payload_sizes == {1024, 65_536, 1_048_576}, (
        f"expected smoke payload sizes {{1024, 65536, 1048576}}, got {payload_sizes}"
    )


def test_default_pipeline_scenarios_no_duplicate_names() -> None:
    """All scenario names in the matrix are unique."""
    scenarios = default_pipeline_scenarios(smoke=False, include_rust=True)
    names = [s.name for s in scenarios]

    assert len(names) == len(set(names)), (
        f"expected all scenario names to be unique but found duplicates in {names}"
    )


def test_default_pipeline_scenarios_python_only_backends() -> None:
    """With ``include_rust=False``, only Python backend scenarios are produced."""
    scenarios = default_pipeline_scenarios(smoke=True, include_rust=False)
    backends = {s.backend for s in scenarios}

    assert backends == {"python"}, (
        f"expected only python backend when include_rust=False, got {backends}"
    )


def test_default_pipeline_scenarios_python_and_rust_backends() -> None:
    """With ``include_rust=True``, both backends are present with 12 each."""
    scenarios = default_pipeline_scenarios(smoke=True, include_rust=True)
    backends = {s.backend for s in scenarios}

    assert backends == {"python", "rust"}, (
        f"expected both python and rust backends, got {backends}"
    )

    python_count = sum(1 for s in scenarios if s.backend == "python")
    rust_count = sum(1 for s in scenarios if s.backend == "rust")
    assert python_count == 12, f"expected 12 python scenarios, got {python_count}"
    assert rust_count == 12, f"expected 12 rust scenarios, got {rust_count}"
