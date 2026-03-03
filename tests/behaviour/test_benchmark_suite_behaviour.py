"""Behavioural tests for benchmark-suite smoke workflow."""

from __future__ import annotations

import json
import subprocess  # noqa: S404  # behavioural test intentionally invokes CLI process
import sys
import typing as typ

import pytest

from benchmarks._test_constants import _SCENARIO_NAME_PATTERN

if typ.TYPE_CHECKING:
    import pathlib as pth

from pytest_bdd import given, scenario, then, when


@scenario(
    "../features/benchmark_suite.feature",
    "Generate a benchmark execution plan in smoke dry-run mode",
)
def test_generate_benchmark_plan_dry_run() -> None:
    """Generate benchmark plans without running expensive benchmarks."""


@scenario(
    "../features/benchmark_suite.feature",
    "Smoke dry-run plan contains the full scenario matrix",
)
def test_smoke_dry_run_contains_full_matrix() -> None:
    """Smoke dry-run plan contains the expected 12-scenario matrix."""


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
        sys.executable,
        "benchmarks/pipeline_throughput.py",
        "--smoke",
        "--dry-run",
        "--output",
        str(benchmark_output_path),
    ]
    try:
        subprocess.run(  # noqa: S603  # command is fixed test input
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"benchmark dry-run command timed out after 30s: {exc.cmd!r}",
        )
    return json.loads(benchmark_output_path.read_text(encoding="utf-8"))


@then("the benchmark plan file exists")
def then_plan_exists(benchmark_output_path: pth.Path) -> None:
    """Assert that the benchmark runner wrote a JSON plan file."""
    assert benchmark_output_path.is_file(), (
        "expected benchmark_output_path to be a file but it does not exist"
    )


@then("the plan includes a Python backend scenario")
def then_python_scenario_exists(benchmark_plan_payload: dict[str, object]) -> None:
    """At least one plan entry targets the Python backend."""
    scenarios = typ.cast("list[dict[str, object]]", benchmark_plan_payload["scenarios"])
    assert any(scenario["backend"] == "python" for scenario in scenarios), (
        "expected at least one scenario targeting the python backend"
    )


@then("the plan records Rust availability")
def then_rust_availability_recorded(
    benchmark_plan_payload: dict[str, object],
) -> None:
    """Assert that the output payload contains Rust availability metadata."""
    assert "rust_available" in benchmark_plan_payload, (
        "expected benchmark plan payload to include rust_available key"
    )
    assert isinstance(benchmark_plan_payload["rust_available"], bool), (
        "expected benchmark plan payload rust_available value to be a boolean"
    )


@then("the benchmark plan indicates a dry run")
def then_benchmark_plan_indicates_dry_run(
    benchmark_plan_payload: dict[str, object],
) -> None:
    """Assert that the benchmark plan was generated in dry-run mode."""
    assert benchmark_plan_payload.get("dry_run") is True, (
        "expected benchmark plan dry_run flag to be True"
    )


@then("the benchmark plan contains valid scenarios")
def then_benchmark_plan_contains_valid_scenarios(
    benchmark_plan_payload: dict[str, object],
) -> None:
    """Assert that the benchmark plan contains well-formed scenarios."""
    scenarios = benchmark_plan_payload.get("scenarios")
    assert isinstance(scenarios, list), "expected benchmark plan scenarios to be a list"
    assert scenarios, "expected at least one scenario in benchmark plan"

    required_keys = {
        "name",
        "backend",
        "payload_bytes",
        "stages",
        "with_line_callbacks",
    }

    for scenario_payload in scenarios:
        assert isinstance(scenario_payload, dict), (
            "expected each benchmark plan scenario to be a dict"
        )
        assert required_keys.issubset(scenario_payload.keys()), (
            "expected each benchmark plan scenario to include required keys"
        )


@then("the benchmark plan includes a valid command")
def then_benchmark_plan_includes_valid_command(
    benchmark_plan_payload: dict[str, object],
) -> None:
    """Assert that the benchmark plan includes an executable command."""
    command = benchmark_plan_payload.get("command")
    assert isinstance(command, list), "expected benchmark plan command to be a list"
    assert command, "expected non-empty command list in benchmark plan"

    for argument in command:
        assert isinstance(argument, str), (
            "expected each benchmark plan command argument to be a string"
        )
        assert argument, "command arguments must be non-empty strings"


# -- Scenario matrix steps (4.4.2) -------------------------------------------


@then("the benchmark plan contains 12 scenarios per backend")
def then_benchmark_plan_contains_12_per_backend(
    benchmark_plan_payload: dict[str, object],
) -> None:
    """Assert 12 scenarios per backend with correct backend distribution."""
    scenarios = typ.cast("list[dict[str, object]]", benchmark_plan_payload["scenarios"])
    rust_available = benchmark_plan_payload.get("rust_available", False)

    # Group by backend.
    counts: dict[str, int] = {}
    for scenario_entry in scenarios:
        backend = typ.cast("str", scenario_entry["backend"])
        counts[backend] = counts.get(backend, 0) + 1

    if rust_available:
        assert set(counts) == {"python", "rust"}, (
            f"expected both python and rust backends when rust_available=True, "
            f"got {set(counts)}"
        )
    else:
        assert set(counts) == {"python"}, (
            f"expected only python backend when rust_available=False, got {set(counts)}"
        )

    for backend, count in counts.items():
        assert count == 12, f"expected 12 scenarios for {backend} backend, got {count}"


@then("every scenario name follows the systematic naming convention")
def then_scenario_names_are_systematic(
    benchmark_plan_payload: dict[str, object],
) -> None:
    """Assert every scenario name matches the naming convention."""
    scenarios = typ.cast("list[dict[str, object]]", benchmark_plan_payload["scenarios"])
    for scenario_entry in scenarios:
        name = typ.cast("str", scenario_entry["name"])
        assert _SCENARIO_NAME_PATTERN.match(name), (
            f"scenario name {name!r} does not match expected pattern "
            f"{_SCENARIO_NAME_PATTERN.pattern!r}"
        )


@then("the scenarios cover all three payload size categories")
def then_scenarios_cover_all_payload_sizes(
    benchmark_plan_payload: dict[str, object],
) -> None:
    """Assert the exact smoke payload sizes (1 KB, 64 KB, 1 MB) are present."""
    scenarios = typ.cast("list[dict[str, object]]", benchmark_plan_payload["scenarios"])
    payload_sizes = {typ.cast("int", s["payload_bytes"]) for s in scenarios}
    assert payload_sizes == {1024, 65_536, 1_048_576}, (
        f"expected smoke payload sizes {{1024, 65536, 1048576}}, got {payload_sizes}"
    )


@then("the scenarios cover both pipeline depths")
def then_scenarios_cover_both_depths(
    benchmark_plan_payload: dict[str, object],
) -> None:
    """Assert both single-stage (2) and multi-stage (3) depths are present."""
    scenarios = typ.cast("list[dict[str, object]]", benchmark_plan_payload["scenarios"])
    stage_counts = {typ.cast("int", s["stages"]) for s in scenarios}
    assert stage_counts == {2, 3}, f"expected stage counts {{2, 3}}, got {stage_counts}"


@then("the scenarios cover both callback modes")
def then_scenarios_cover_both_callback_modes(
    benchmark_plan_payload: dict[str, object],
) -> None:
    """Assert both with and without line callbacks are present."""
    scenarios = typ.cast("list[dict[str, object]]", benchmark_plan_payload["scenarios"])
    callback_modes = {typ.cast("bool", s["with_line_callbacks"]) for s in scenarios}
    assert callback_modes == {True, False}, (
        f"expected callback modes {{True, False}}, got {callback_modes}"
    )
