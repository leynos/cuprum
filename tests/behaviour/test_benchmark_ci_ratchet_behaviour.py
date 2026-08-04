"""Behavioural tests for benchmark CI Rust ratchet CLI."""

from __future__ import annotations

import json
import subprocess  # noqa: S404  # behavioural test intentionally invokes CLI process
import sys
import typing as typ

from tests.behaviour._benchmark_ratchet_support import (
    FixtureBundle,
    _plan_payload,
    _prepare_fixture_bundle,
    _scenario_payload,
    _write_json,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

from pytest_bdd import given, scenario, then, when


class CliResult(typ.TypedDict):
    """Typed CLI result payload for benchmark ratchet behaviour tests."""

    completed: subprocess.CompletedProcess[str]
    report_path: pth.Path


@scenario(
    "../features/benchmark_ci_ratchet.feature",
    "Ratchet passes when Rust regression stays within threshold",
)
def test_ratchet_passes_within_threshold() -> None:
    """CLI should pass when Rust slowdown is not greater than threshold."""


@scenario(
    "../features/benchmark_ci_ratchet.feature",
    "Ratchet fails when Rust regression exceeds threshold",
)
def test_ratchet_fails_above_threshold() -> None:
    """CLI should fail when Rust slowdown breaches threshold."""


@scenario(
    "../features/benchmark_ci_ratchet.feature",
    "Ratchet reports malformed inputs as configuration errors",
)
def test_ratchet_reports_malformed_inputs() -> None:
    """CLI should return the malformed-input exit code for invalid fixtures."""


@given(
    "benchmark comparison fixtures where candidate stays within threshold",
    target_fixture="ratchet_fixture_bundle",
)
def given_candidate_within_threshold(tmp_path: pth.Path) -> FixtureBundle:
    """Create fixture JSON files with a 10% Rust/Python ratio increase (passes)."""
    return _prepare_fixture_bundle(tmp_path=tmp_path, candidate_rust_mean=2.20)


@given(
    "benchmark comparison fixtures where candidate exceeds threshold",
    target_fixture="ratchet_fixture_bundle",
)
def given_candidate_exceeds_threshold(tmp_path: pth.Path) -> FixtureBundle:
    """Create fixture JSON files with a 25% Rust/Python ratio increase (fails)."""
    return _prepare_fixture_bundle(tmp_path=tmp_path, candidate_rust_mean=2.50)


@given(
    "malformed benchmark comparison fixtures",
    target_fixture="ratchet_fixture_bundle",
)
def given_malformed_fixtures(tmp_path: pth.Path) -> FixtureBundle:
    """Create fixture JSON files that trigger the CLI malformed-input path."""
    fixture_bundle = _prepare_fixture_bundle(
        tmp_path=tmp_path,
        candidate_rust_mean=1.0,
    )
    _write_json(
        path=fixture_bundle["candidate_plan_path"],
        payload=_plan_payload(
            scenarios=[
                _scenario_payload(name="python-small-single-nocb", backend="python"),
                # ``native`` is not a valid backend: this is the malformation
                # that drives the CLI's malformed-input exit path.
                _scenario_payload(name="rust-small-single-nocb", backend="native"),
            ],
        ),
    )
    return fixture_bundle


@when("I run the Rust benchmark ratchet CLI", target_fixture="ratchet_cli_result")
def when_run_ratchet_cli(
    ratchet_fixture_bundle: FixtureBundle,
) -> CliResult:
    """Execute the ratchet CLI against prepared baseline/candidate fixtures."""
    command = [
        sys.executable,
        "benchmarks/ratchet_rust_performance.py",
        "--baseline-plan",
        str(ratchet_fixture_bundle["baseline_plan_path"]),
        "--baseline-throughput",
        str(ratchet_fixture_bundle["baseline_throughput_path"]),
        "--candidate-plan",
        str(ratchet_fixture_bundle["candidate_plan_path"]),
        "--candidate-throughput",
        str(ratchet_fixture_bundle["candidate_throughput_path"]),
        "--max-regression",
        "0.10",
        "--output",
        str(ratchet_fixture_bundle["report_path"]),
    ]
    completed = subprocess.run(  # noqa: S603  # command is fixed test input
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return {
        "completed": completed,
        "report_path": ratchet_fixture_bundle["report_path"],
    }


def _assert_returncode(ratchet_cli_result: CliResult, *, expected: int) -> None:
    """Assert the CLI return code and emit stdout/stderr for failures."""
    completed = ratchet_cli_result["completed"]
    assert completed.returncode == expected, (
        f"expected ratchet to exit with code {expected}, got "
        f"{completed.returncode}:\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )


@then("the ratchet command exits successfully")
def then_ratchet_exits_successfully(
    ratchet_cli_result: CliResult,
) -> None:
    """CLI should return zero for within-threshold regression."""
    _assert_returncode(ratchet_cli_result, expected=0)


@then("the ratchet command exits with failure")
def then_ratchet_exits_with_failure(
    ratchet_cli_result: CliResult,
) -> None:
    """CLI should return non-zero for above-threshold regression."""
    _assert_returncode(ratchet_cli_result, expected=1)


@then("the ratchet command exits with malformed-input failure")
def then_ratchet_exits_with_malformed_input_failure(
    ratchet_cli_result: CliResult,
) -> None:
    """CLI should return 2 when the benchmark inputs are malformed."""
    _assert_returncode(ratchet_cli_result, expected=2)


@then("the ratchet report indicates success")
def then_ratchet_report_indicates_success(
    ratchet_cli_result: CliResult,
) -> None:
    """Report JSON should indicate the comparison passed."""
    payload = json.loads(ratchet_cli_result["report_path"].read_text(encoding="utf-8"))

    assert payload["passed"] is True, "expected ratchet report passed=True"
    assert payload["rust_scenarios_compared"] == 1, (
        "expected exactly one Rust scenario in the fixture comparison"
    )


@then("the ratchet report indicates regression failure")
def then_ratchet_report_indicates_failure(
    ratchet_cli_result: CliResult,
) -> None:
    """Report JSON should indicate the comparison failed with regressions."""
    payload = json.loads(ratchet_cli_result["report_path"].read_text(encoding="utf-8"))

    assert payload["passed"] is False, "expected ratchet report passed=False"
    regressions = payload["regressions"]
    assert isinstance(regressions, list), "expected regressions to be a list"
    assert regressions, "expected at least one failed Rust scenario"
