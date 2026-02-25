"""Behavioural tests for benchmark-suite smoke workflow."""

from __future__ import annotations

import json
import subprocess  # noqa: S404  # behavioural test intentionally invokes CLI process
import typing as typ

if typ.TYPE_CHECKING:
    import pathlib as pth

from pytest_bdd import given, scenario, then, when


@scenario(
    "../features/benchmark_suite.feature",
    "Generate a benchmark execution plan in smoke dry-run mode",
)
def test_generate_benchmark_plan_dry_run() -> None:
    """Generate benchmark plans without running expensive benchmarks."""


@given("a benchmark output path", target_fixture="benchmark_output_path")
def given_output_path(tmp_path: pth.Path) -> pth.Path:
    """Provide an output path for benchmark plan JSON."""
    return tmp_path / "benchmark-plan.json"


@when(
    "I generate benchmark plans in smoke dry-run mode",
    target_fixture="benchmark_plan_payload",
)
def when_generate_plans(
    benchmark_output_path: pth.Path,
) -> dict[str, object]:
    """Run the benchmark CLI in dry-run mode and parse JSON output."""
    command = [
        "uv",
        "run",
        "python",
        "benchmarks/pipeline_throughput.py",
        "--smoke",
        "--dry-run",
        "--output",
        str(benchmark_output_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)  # noqa: S603  # command is fixed test input
    return json.loads(benchmark_output_path.read_text(encoding="utf-8"))


@then("the benchmark plan file exists")
def then_plan_exists(benchmark_output_path: pth.Path) -> None:
    """Assert that the benchmark runner wrote a JSON plan file."""
    assert benchmark_output_path.is_file()


@then("the plan includes a Python backend scenario")
def then_python_scenario_exists(benchmark_plan_payload: dict[str, object]) -> None:
    """At least one plan entry targets the Python backend."""
    scenarios = typ.cast("list[dict[str, object]]", benchmark_plan_payload["scenarios"])
    assert any(scenario["backend"] == "python" for scenario in scenarios)


@then("the plan records Rust availability")
def then_rust_availability_recorded(
    benchmark_plan_payload: dict[str, object],
) -> None:
    """Assert that the output payload contains Rust availability metadata."""
    assert "rust_available" in benchmark_plan_payload
