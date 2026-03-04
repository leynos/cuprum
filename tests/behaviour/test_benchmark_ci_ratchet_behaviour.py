"""Behavioural tests for benchmark CI Rust ratchet CLI."""

from __future__ import annotations

import json
import subprocess  # noqa: S404  # behavioural test intentionally invokes CLI process
import sys
import typing as typ

if typ.TYPE_CHECKING:
    import pathlib as pth

from pytest_bdd import given, scenario, then, when


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


def _scenario_payload(*, name: str, backend: str) -> dict[str, object]:
    return {
        "name": name,
        "backend": backend,
        "payload_bytes": 1024,
        "stages": 2,
        "with_line_callbacks": False,
    }


def _plan_payload() -> dict[str, object]:
    return {
        "dry_run": True,
        "rust_available": True,
        "command": ["hyperfine", "placeholder"],
        "scenarios": [
            _scenario_payload(name="python-small-single-nocb", backend="python"),
            _scenario_payload(name="rust-small-single-nocb", backend="rust"),
        ],
    }


def _throughput_payload(*, python_mean: float, rust_mean: float) -> dict[str, object]:
    return {
        "results": [
            {"command": "python-run", "mean": python_mean},
            {"command": "rust-run", "mean": rust_mean},
        ],
    }


def _write_json(
    *,
    path: pth.Path,
    payload: dict[str, object],
) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _prepare_fixture_bundle(
    *,
    tmp_path: pth.Path,
    candidate_rust_mean: float,
) -> dict[str, pth.Path]:
    baseline_plan_path = tmp_path / "baseline-plan.json"
    baseline_throughput_path = tmp_path / "baseline-throughput.json"
    candidate_plan_path = tmp_path / "candidate-plan.json"
    candidate_throughput_path = tmp_path / "candidate-throughput.json"
    report_path = tmp_path / "ratchet-report.json"

    _write_json(path=baseline_plan_path, payload=_plan_payload())
    _write_json(
        path=baseline_throughput_path,
        payload=_throughput_payload(python_mean=0.2, rust_mean=1.0),
    )
    _write_json(path=candidate_plan_path, payload=_plan_payload())
    _write_json(
        path=candidate_throughput_path,
        payload=_throughput_payload(python_mean=5.0, rust_mean=candidate_rust_mean),
    )

    return {
        "baseline_plan_path": baseline_plan_path,
        "baseline_throughput_path": baseline_throughput_path,
        "candidate_plan_path": candidate_plan_path,
        "candidate_throughput_path": candidate_throughput_path,
        "report_path": report_path,
    }


@given(
    "benchmark comparison fixtures where candidate stays within threshold",
    target_fixture="ratchet_fixture_bundle",
)
def given_candidate_within_threshold(tmp_path: pth.Path) -> dict[str, pth.Path]:
    """Create fixture JSON files with a 10% Rust slowdown (passes)."""
    return _prepare_fixture_bundle(tmp_path=tmp_path, candidate_rust_mean=1.10)


@given(
    "benchmark comparison fixtures where candidate exceeds threshold",
    target_fixture="ratchet_fixture_bundle",
)
def given_candidate_exceeds_threshold(tmp_path: pth.Path) -> dict[str, pth.Path]:
    """Create fixture JSON files with a 25% Rust slowdown (fails)."""
    return _prepare_fixture_bundle(tmp_path=tmp_path, candidate_rust_mean=1.25)


@when("I run the Rust benchmark ratchet CLI", target_fixture="ratchet_cli_result")
def when_run_ratchet_cli(
    ratchet_fixture_bundle: dict[str, pth.Path],
) -> dict[str, object]:
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


@then("the ratchet command exits successfully")
def then_ratchet_exits_successfully(
    ratchet_cli_result: dict[str, object],
) -> None:
    """CLI should return zero for within-threshold regression."""
    completed = typ.cast(
        "subprocess.CompletedProcess[str]", ratchet_cli_result["completed"]
    )
    assert completed.returncode == 0, (
        "expected successful ratchet exit code but got "
        f"{completed.returncode}:\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )


@then("the ratchet command exits with failure")
def then_ratchet_exits_with_failure(
    ratchet_cli_result: dict[str, object],
) -> None:
    """CLI should return non-zero for above-threshold regression."""
    completed = typ.cast(
        "subprocess.CompletedProcess[str]", ratchet_cli_result["completed"]
    )
    assert completed.returncode == 1, (
        "expected ratchet to fail with exit code 1 when threshold is breached; "
        "got "
        f"{completed.returncode}:\nstdout={completed.stdout}\nstderr={completed.stderr}"
    )


@then("the ratchet report indicates success")
def then_ratchet_report_indicates_success(
    ratchet_cli_result: dict[str, object],
) -> None:
    """Report JSON should indicate the comparison passed."""
    report_path = typ.cast("pth.Path", ratchet_cli_result["report_path"])
    payload = json.loads(report_path.read_text(encoding="utf-8"))

    assert payload["passed"] is True, "expected ratchet report passed=True"
    assert payload["rust_scenarios_compared"] == 1, (
        "expected exactly one Rust scenario in the fixture comparison"
    )


@then("the ratchet report indicates regression failure")
def then_ratchet_report_indicates_failure(
    ratchet_cli_result: dict[str, object],
) -> None:
    """Report JSON should indicate the comparison failed with regressions."""
    report_path = typ.cast("pth.Path", ratchet_cli_result["report_path"])
    payload = json.loads(report_path.read_text(encoding="utf-8"))

    assert payload["passed"] is False, "expected ratchet report passed=False"
    regressions = payload["regressions"]
    assert isinstance(regressions, list), "expected regressions to be a list"
    assert regressions, "expected at least one failed Rust scenario"
