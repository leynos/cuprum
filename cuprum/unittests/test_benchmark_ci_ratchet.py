"""Unit tests for benchmark CI ratchet comparison helpers."""

from __future__ import annotations

import json
import typing as typ

import pytest

from benchmarks.ratchet_rust_performance import (
    BenchmarkRunPayload,
    ComparisonReport,
    compare_rust_regressions,
    load_plan,
    load_throughput,
)

if typ.TYPE_CHECKING:
    import pathlib as pth


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
    tmp_path: pth.Path,
    filename: str,
    payload: dict[str, object],
) -> pth.Path:
    path = tmp_path / filename
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class _RunMeans(typ.NamedTuple):
    """Python and Rust mean runtimes for one benchmark run."""

    python: float
    rust: float


def _run_comparison(
    *,
    baseline: _RunMeans,
    candidate: _RunMeans,
    max_regression: float = 0.10,
) -> ComparisonReport:
    """Build baseline/candidate payloads and run the Rust ratchet comparison."""
    return compare_rust_regressions(
        baseline=BenchmarkRunPayload(
            plan=_plan_payload(),
            throughput=_throughput_payload(
                python_mean=baseline.python,
                rust_mean=baseline.rust,
            ),
            context_name="baseline",
        ),
        candidate=BenchmarkRunPayload(
            plan=_plan_payload(),
            throughput=_throughput_payload(
                python_mean=candidate.python,
                rust_mean=candidate.rust,
            ),
            context_name="candidate",
        ),
        max_regression=max_regression,
    )


def test_load_plan_rejects_missing_scenarios(tmp_path: pth.Path) -> None:
    """Plan payloads must include a scenarios list."""
    path = _write_json(
        tmp_path=tmp_path,
        filename="plan.json",
        payload={"dry_run": True, "command": ["hyperfine"]},
    )

    with pytest.raises(TypeError, match="scenarios"):
        load_plan(path)


def test_load_throughput_rejects_missing_results(tmp_path: pth.Path) -> None:
    """Throughput payloads must include a results list."""
    path = _write_json(
        tmp_path=tmp_path,
        filename="throughput.json",
        payload={"meta": {}},
    )

    with pytest.raises(TypeError, match="results"):
        load_throughput(path)


def test_compare_rust_regressions_passes_within_threshold() -> None:
    """A Rust slowdown at or under 10% should pass the ratchet."""
    report = _run_comparison(
        baseline=_RunMeans(python=0.50, rust=1.00),
        candidate=_RunMeans(python=1.50, rust=1.10),
    )

    assert report.passed is True
    assert report.rust_scenarios_compared == 1
    assert len(report.comparisons) == 1
    assert report.comparisons[0].scenario_name == "rust-small-single-nocb"
    assert report.comparisons[0].regression_ratio == pytest.approx(0.10)


def test_compare_rust_regressions_fails_beyond_threshold() -> None:
    """A Rust slowdown above 10% should fail the ratchet."""
    report = _run_comparison(
        baseline=_RunMeans(python=0.25, rust=1.00),
        candidate=_RunMeans(python=0.25, rust=1.25),
    )

    assert report.passed is False
    assert report.worst_regression_ratio == pytest.approx(0.25)
    assert len(report.regressions) == 1
    assert report.regressions[0].scenario_name == "rust-small-single-nocb"


def test_compare_rust_regressions_rejects_result_count_mismatch() -> None:
    """Plan/results length mismatches should fail fast."""
    candidate_throughput = {
        "results": [
            {"command": "python-only", "mean": 1.0},
        ],
    }

    with pytest.raises(ValueError, match="must match"):
        compare_rust_regressions(
            baseline=BenchmarkRunPayload(
                plan=_plan_payload(),
                throughput=_throughput_payload(python_mean=1.0, rust_mean=1.0),
                context_name="baseline",
            ),
            candidate=BenchmarkRunPayload(
                plan=_plan_payload(),
                throughput=candidate_throughput,
                context_name="candidate",
            ),
            max_regression=0.10,
        )


def test_compare_rust_regressions_rejects_missing_rust_scenarios() -> None:
    """Ratchet should fail if there are no Rust scenarios to compare."""
    python_only_plan = {
        "dry_run": True,
        "rust_available": False,
        "command": ["hyperfine", "placeholder"],
        "scenarios": [
            _scenario_payload(name="python-small-single-nocb", backend="python"),
        ],
    }
    python_only_throughput = {
        "results": [
            {"command": "python-run", "mean": 1.0},
        ],
    }

    with pytest.raises(ValueError, match="Rust"):
        compare_rust_regressions(
            baseline=BenchmarkRunPayload(
                plan=python_only_plan,
                throughput=python_only_throughput,
                context_name="baseline",
            ),
            candidate=BenchmarkRunPayload(
                plan=python_only_plan,
                throughput=python_only_throughput,
                context_name="candidate",
            ),
            max_regression=0.10,
        )
