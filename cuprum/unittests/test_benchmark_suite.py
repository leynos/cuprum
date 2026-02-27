"""Unit tests for benchmark suite command and scenario helpers."""

from __future__ import annotations

import json
import pathlib as pth
import typing as typ

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
