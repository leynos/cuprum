"""Unit tests for benchmark suite command and scenario helpers."""

from __future__ import annotations

import json
import pathlib as pth

import pytest

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

    assert any(s.backend == "python" for s in scenarios)


def test_default_pipeline_scenarios_gate_rust_backend() -> None:
    """Rust scenarios are omitted when ``include_rust`` is ``False``."""
    scenarios = default_pipeline_scenarios(smoke=True, include_rust=False)

    assert all(s.backend != "rust" for s in scenarios)


def test_render_prefixed_command_prefixes_env_and_quotes_tokens() -> None:
    """The rendered command prefixes env vars and safely quotes tokens."""
    command = ["uv", "run", "python", "benchmarks/pipeline_worker.py", "--label", "a b"]
    rendered = render_prefixed_command(
        command=command,
        env={"CUPRUM_STREAM_BACKEND": "python"},
    )

    assert rendered.startswith("CUPRUM_STREAM_BACKEND=python ")
    assert "'a b'" in rendered


def test_render_prefixed_command_with_empty_env_returns_raw_command() -> None:
    """When env is empty, rendered command should contain no env prefix."""
    command = ["uv", "run", "python", "benchmarks/pipeline_worker.py"]

    rendered = render_prefixed_command(command=command, env={})

    assert rendered == "uv run python benchmarks/pipeline_worker.py"


def test_hyperfine_config_rejects_negative_warmup() -> None:
    """Warmup must be zero or positive."""
    with pytest.raises(ValueError, match="warmup must be >= 0"):
        HyperfineConfig(warmup=-1, runs=1)


def test_hyperfine_config_rejects_non_positive_runs() -> None:
    """Runs must be at least one."""
    with pytest.raises(ValueError, match="runs must be >= 1"):
        HyperfineConfig(warmup=0, runs=0)


def test_pipeline_benchmark_scenario_rejects_invalid_backend() -> None:
    """Scenario backend must be one of the supported stream backends."""
    with pytest.raises(ValueError, match="backend must be one of"):
        PipelineBenchmarkScenario(
            name="pipeline-typo",
            backend="pythno",  # type: ignore[arg-type]
            payload_bytes=1024,
            stages=2,
            with_line_callbacks=False,
        )


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

    assert command[0].endswith("hyperfine")
    assert "--export-json" in command
    assert "--warmup" in command
    assert "--runs" in command
    assert any("CUPRUM_STREAM_BACKEND=python" in token for token in command)


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

    assert result.dry_run is True
    assert output_path.is_file()
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert payload["dry_run"] is True
    assert "rust_available" in payload
    assert isinstance(payload["rust_available"], bool)

    scenarios_payload = payload["scenarios"]
    assert isinstance(scenarios_payload, list)
    assert scenarios_payload
    assert any(
        isinstance(scenario, dict)
        and scenario.get("backend") == "python"
        and scenario.get("name") == "pipeline-python"
        for scenario in scenarios_payload
    )

    command_payload = payload["command"]
    assert isinstance(command_payload, list)
    assert command_payload
    assert all(isinstance(argument, str) for argument in command_payload)
