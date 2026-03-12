"""Behavioural tests for benchmark comparison report CLI output."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - behavioural test intentionally invokes CLI process
import sys
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

if typ.TYPE_CHECKING:
    import pathlib as pth


class FixtureBundle(typ.TypedDict):
    """Typed fixture bundle for one comparison-report CLI invocation."""

    candidate_plan_path: pth.Path
    candidate_throughput_path: pth.Path
    ratchet_report_path: pth.Path
    output_json_path: pth.Path
    output_markdown_path: pth.Path


class CliResult(typ.TypedDict):
    """Typed CLI result payload."""

    completed: subprocess.CompletedProcess[str]
    output_json_path: pth.Path
    output_markdown_path: pth.Path


@scenario(
    "../features/benchmark_comparison_report.feature",
    "Generate a benchmark comparison report and markdown summary",
)
def test_generate_benchmark_comparison_report() -> None:
    """CLI should produce JSON and Markdown for valid paired benchmark data."""


@scenario(
    "../features/benchmark_comparison_report.feature",
    "Reject malformed benchmark comparison artefacts",
)
def test_reject_malformed_benchmark_comparison_report() -> None:
    """CLI should fail for malformed or unpaired benchmark data."""


def _scenario_payload(
    *,
    name: str,
    backend: str,
    with_line_callbacks: bool = False,
) -> dict[str, object]:
    """Create one benchmark scenario payload."""
    return {
        "name": name,
        "backend": backend,
        "payload_bytes": 1024,
        "stages": 2,
        "with_line_callbacks": with_line_callbacks,
    }


def _write_json(path: pth.Path, payload: dict[str, object]) -> None:
    """Write one JSON payload."""
    path.write_text(json.dumps(payload), encoding="utf-8")


def _prepare_fixture_bundle(
    *,
    tmp_path: pth.Path,
    malformed: bool,
) -> FixtureBundle:
    """Create CLI fixtures for valid or malformed benchmark comparison data."""
    candidate_plan_path = tmp_path / "candidate-plan.json"
    candidate_throughput_path = tmp_path / "candidate-throughput.json"
    ratchet_report_path = tmp_path / "ratchet-report.json"
    output_json_path = tmp_path / "comparison-report.json"
    output_markdown_path = tmp_path / "comparison-summary.md"

    scenarios: list[dict[str, object]] = [
        _scenario_payload(name="python-small-single-nocb", backend="python"),
        _scenario_payload(name="rust-small-single-nocb", backend="rust"),
    ]
    results: list[dict[str, object]] = [
        {"command": "python-small-single-nocb", "mean": 0.42},
        {"command": "rust-small-single-nocb", "mean": 0.21},
    ]

    if malformed:
        scenarios.pop()
        results.pop()

    _write_json(
        candidate_plan_path,
        {
            "dry_run": True,
            "rust_available": True,
            "command": ["hyperfine", "placeholder"],
            "scenarios": scenarios,
        },
    )
    _write_json(candidate_throughput_path, {"results": results})
    _write_json(
        ratchet_report_path,
        {
            "baseline_available": False,
            "comparison_performed": False,
            "passed": True,
            "reason": "no_previous_main_benchmark_baseline",
        },
    )

    return {
        "candidate_plan_path": candidate_plan_path,
        "candidate_throughput_path": candidate_throughput_path,
        "ratchet_report_path": ratchet_report_path,
        "output_json_path": output_json_path,
        "output_markdown_path": output_markdown_path,
    }


@given(
    "paired candidate benchmark artefacts",
    target_fixture="comparison_fixture_bundle",
)
def given_paired_candidate_benchmark_artefacts(
    tmp_path: pth.Path,
) -> FixtureBundle:
    """Create valid benchmark comparison inputs."""
    return _prepare_fixture_bundle(tmp_path=tmp_path, malformed=False)


@given(
    "malformed candidate benchmark artefacts",
    target_fixture="comparison_fixture_bundle",
)
def given_malformed_candidate_benchmark_artefacts(
    tmp_path: pth.Path,
) -> FixtureBundle:
    """Create malformed benchmark comparison inputs."""
    return _prepare_fixture_bundle(tmp_path=tmp_path, malformed=True)


@when(
    "I run the benchmark comparison report CLI",
    target_fixture="comparison_cli_result",
)
def when_run_benchmark_comparison_report_cli(
    comparison_fixture_bundle: FixtureBundle,
) -> CliResult:
    """Execute the benchmark comparison-report CLI."""
    command = [
        sys.executable,
        "benchmarks/python_vs_rust_comparison_report.py",
        "--plan",
        str(comparison_fixture_bundle["candidate_plan_path"]),
        "--throughput",
        str(comparison_fixture_bundle["candidate_throughput_path"]),
        "--ratchet-report",
        str(comparison_fixture_bundle["ratchet_report_path"]),
        "--output-json",
        str(comparison_fixture_bundle["output_json_path"]),
        "--output-markdown",
        str(comparison_fixture_bundle["output_markdown_path"]),
    ]
    completed = subprocess.run(  # noqa: S603 - fixed test command
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return {
        "completed": completed,
        "output_json_path": comparison_fixture_bundle["output_json_path"],
        "output_markdown_path": comparison_fixture_bundle["output_markdown_path"],
    }


def _assert_returncode(comparison_cli_result: CliResult, *, expected: int) -> None:
    """Assert the CLI return code and include stdout/stderr on failure."""
    completed = comparison_cli_result["completed"]
    assert completed.returncode == expected, (
        f"expected comparison report CLI to exit with code {expected}, got "
        f"{completed.returncode}:\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )


@then("the comparison report command exits successfully")
def then_comparison_report_command_exits_successfully(
    comparison_cli_result: CliResult,
) -> None:
    """Successful comparison-report CLI runs should exit zero."""
    _assert_returncode(comparison_cli_result, expected=0)


@then("the comparison report command exits with malformed-input failure")
def then_comparison_report_command_exits_with_malformed_input_failure(
    comparison_cli_result: CliResult,
) -> None:
    """Malformed comparison-report CLI runs should exit two."""
    _assert_returncode(comparison_cli_result, expected=2)


@then("the comparison report JSON contains paired backend rows")
def then_comparison_report_json_contains_paired_backend_rows(
    comparison_cli_result: CliResult,
) -> None:
    """JSON output should contain one paired Python/Rust comparison row."""
    payload = json.loads(
        comparison_cli_result["output_json_path"].read_text(encoding="utf-8")
    )
    assert payload["summary"]["row_count"] == 1, "expected exactly one comparison row"
    row = payload["rows"][0]
    assert row["comparison_id"] == "small-single-nocb"
    assert row["python_mean"] == pytest.approx(0.42)
    assert row["rust_mean"] == pytest.approx(0.21)
    assert row["faster_backend"] == "rust"


@then("the comparison summary markdown contains a workflow table")
def then_comparison_summary_markdown_contains_a_workflow_table(
    comparison_cli_result: CliResult,
) -> None:
    """Markdown output should be ready for `$GITHUB_STEP_SUMMARY`."""
    markdown = comparison_cli_result["output_markdown_path"].read_text(encoding="utf-8")
    assert "## Python vs Rust benchmark comparison" in markdown
    assert "Rust regression ratchet skipped" in markdown
    assert (
        "| Scenario | Python mean (s) | Rust mean (s) | Speedup | Faster backend |"
        in markdown
    )
    assert "| `small-single-nocb` | 0.420000 | 0.210000 | 2.00x | rust |" in markdown
