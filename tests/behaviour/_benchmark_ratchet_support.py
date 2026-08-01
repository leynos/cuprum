"""Fixture-payload builders for the benchmark CI ratchet behaviour tests.

This module holds the plan/throughput payload construction shared by the
ratchet scenarios, keeping the collected behaviour module within the
project's per-file line limit. It carries a leading underscore so pytest
does not collect it as a test module.
"""

from __future__ import annotations

import json
import typing as typ

from benchmarks.benchmark_profile import BENCHMARK_PROFILE_VERSION

if typ.TYPE_CHECKING:
    import pathlib as pth


class FixtureBundle(typ.TypedDict):
    """Typed fixture paths for one ratchet CLI invocation."""

    baseline_plan_path: pth.Path
    baseline_throughput_path: pth.Path
    candidate_plan_path: pth.Path
    candidate_throughput_path: pth.Path
    report_path: pth.Path


def _scenario_payload(*, name: str, backend: str) -> dict[str, object]:
    """Create scenario payload."""
    return {
        "name": name,
        "backend": backend,
        "payload_bytes": 1024,
        "stages": 2,
        "with_line_callbacks": False,
    }


def _plan_payload() -> dict[str, object]:
    """Create plan payload."""
    return {
        "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
        "dry_run": True,
        "rust_available": True,
        "worker_iterations": 20,
        "command": ["hyperfine", "placeholder"],
        "scenarios": [
            _scenario_payload(name="python-small-single-nocb", backend="python"),
            _scenario_payload(name="rust-small-single-nocb", backend="rust"),
        ],
    }


def _throughput_payload(*, python_mean: float, rust_mean: float) -> dict[str, object]:
    """Create throughput payload."""
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
    """Write JSON payload."""
    path.write_text(json.dumps(payload), encoding="utf-8")


def _prepare_fixture_bundle(
    *,
    tmp_path: pth.Path,
    candidate_rust_mean: float,
) -> FixtureBundle:
    """Create ratchet fixture bundle."""
    baseline_plan_path = tmp_path / "baseline-plan.json"
    baseline_throughput_path = tmp_path / "baseline-throughput.json"
    candidate_plan_path = tmp_path / "candidate-plan.json"
    candidate_throughput_path = tmp_path / "candidate-throughput.json"
    report_path = tmp_path / "ratchet-report.json"

    # The candidate run doubles the Python mean (a uniformly slower runner);
    # only the Rust/Python ratio relative to the baseline's 1.0 should count.
    _write_json(path=baseline_plan_path, payload=_plan_payload())
    _write_json(
        path=baseline_throughput_path,
        payload=_throughput_payload(python_mean=1.0, rust_mean=1.0),
    )
    _write_json(path=candidate_plan_path, payload=_plan_payload())
    _write_json(
        path=candidate_throughput_path,
        payload=_throughput_payload(python_mean=2.0, rust_mean=candidate_rust_mean),
    )

    return {
        "baseline_plan_path": baseline_plan_path,
        "baseline_throughput_path": baseline_throughput_path,
        "candidate_plan_path": candidate_plan_path,
        "candidate_throughput_path": candidate_throughput_path,
        "report_path": report_path,
    }
