"""Unit tests for benchmark suite command and scenario helpers."""

from __future__ import annotations

import pathlib as pth

from benchmarks.pipeline_throughput import (
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
    payload = output_path.read_text(encoding="utf-8")
    assert "pipeline-python" in payload
    assert "hyperfine" in payload
