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


class ScenarioPayload(typ.TypedDict):
    """Serialized benchmark scenario fixture.

    Attributes
    ----------
    name : str
        Scenario name used to pair plan and throughput entries.
    backend : str
        Stream backend selected for the scenario.
    payload_bytes : int
        Number of bytes processed by each pipeline stage.
    stages : int
        Number of pipeline stages in the scenario.
    with_line_callbacks : bool
        Whether the scenario enables line callbacks.
    """

    name: str
    backend: str
    payload_bytes: int
    stages: int
    with_line_callbacks: bool


class PlanPayload(typ.TypedDict):
    """Serialized benchmark plan fixture.

    Attributes
    ----------
    benchmark_profile_version : str
        Version of the benchmark profile schema.
    dry_run : bool
        Whether the benchmark plan represents a dry run.
    rust_available : bool
        Whether the Rust stream backend is available.
    worker_iterations : int
        Number of iterations performed by each benchmark worker.
    command : list[str]
        Command arguments used to invoke the benchmark runner.
    scenarios : list[ScenarioPayload]
        Ordered benchmark scenarios in the plan.
    """

    benchmark_profile_version: str
    dry_run: bool
    rust_available: bool
    worker_iterations: int
    command: list[str]
    scenarios: list[ScenarioPayload]


class ThroughputResultPayload(typ.TypedDict):
    """Serialized hyperfine result fixture.

    Attributes
    ----------
    command : str
        Command associated with the throughput measurement.
    mean : float
        Mean execution time reported by hyperfine.
    """

    command: str
    mean: float


class ThroughputPayload(typ.TypedDict):
    """Serialized hyperfine payload fixture.

    Attributes
    ----------
    results : list[ThroughputResultPayload]
        Ordered throughput results emitted by hyperfine.
    """

    results: list[ThroughputResultPayload]


def _scenario_payload(*, name: str, backend: str) -> ScenarioPayload:
    """Create scenario payload."""
    return {
        "name": name,
        "backend": backend,
        "payload_bytes": 1024,
        "stages": 2,
        "with_line_callbacks": False,
    }


def _plan_payload(scenarios: list[ScenarioPayload] | None = None) -> PlanPayload:
    """Create plan payload, optionally overriding the default scenarios."""
    selected_scenarios = scenarios
    if selected_scenarios is None:
        selected_scenarios = [
            _scenario_payload(name="python-small-single-nocb", backend="python"),
            _scenario_payload(name="rust-small-single-nocb", backend="rust"),
        ]
    return {
        "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
        "dry_run": True,
        "rust_available": True,
        "worker_iterations": 20,
        "command": ["hyperfine", "placeholder"],
        "scenarios": selected_scenarios,
    }


def _throughput_payload(*, python_mean: float, rust_mean: float) -> ThroughputPayload:
    """Create throughput payload."""
    return {
        "results": [
            {"command": "python-small-single-nocb", "mean": python_mean},
            {"command": "rust-small-single-nocb", "mean": rust_mean},
        ],
    }


def _write_json(
    *,
    path: pth.Path,
    payload: PlanPayload | ThroughputPayload,
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
