"""Unit tests for loading validated benchmark-ratchet input payloads."""

from __future__ import annotations

import json
import typing as typ

import pytest

from benchmarks.benchmark_profile import (
    BENCHMARK_PROFILE_VERSION,
    IncompatibleBenchmarkProfileError,
)
from benchmarks.ratchet_rust_performance import load_plan, load_throughput

if typ.TYPE_CHECKING:
    import pathlib as pth


def _scenario_payload(*, name: str, backend: str) -> dict[str, object]:
    """Return one benchmark scenario payload."""
    return {
        "name": name,
        "backend": backend,
        "payload_bytes": 1024,
        "stages": 2,
        "with_line_callbacks": False,
    }


def _plan_payload() -> dict[str, object]:
    """Return a valid benchmark plan payload."""
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


def _write_json(
    *, tmp_path: pth.Path, filename: str, payload: dict[str, object]
) -> pth.Path:
    """Write a JSON payload to a temporary test file."""
    path = tmp_path / filename
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_plan_rejects_missing_scenarios(tmp_path: pth.Path) -> None:
    """Plan payloads must include a scenarios list."""
    path = _write_json(
        tmp_path=tmp_path,
        filename="plan.json",
        payload={
            "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
            "dry_run": True,
            "worker_iterations": 20,
            "command": ["hyperfine"],
        },
    )

    with pytest.raises(TypeError, match="scenarios"):
        load_plan(path)


def test_load_plan_rejects_legacy_profile_metadata(tmp_path: pth.Path) -> None:
    """Old single-run benchmark plans are incompatible with batched results."""
    path = _write_json(
        tmp_path=tmp_path,
        filename="plan.json",
        payload={
            "dry_run": True,
            "rust_available": True,
            "command": ["hyperfine", "placeholder"],
            "scenarios": [
                _scenario_payload(name="rust-small-single-nocb", backend="rust"),
            ],
        },
    )

    with pytest.raises(IncompatibleBenchmarkProfileError, match="missing"):
        load_plan(path)


@pytest.mark.parametrize(
    "profile_version",
    [
        pytest.param("pipeline-worker-single-run-v1", id="old_profile_version"),
        pytest.param(
            "pipeline-worker-release-ratio-v3",
            id="immediate_predecessor_profile_version",
        ),
    ],
)
def test_load_plan_rejects_incompatible_profile_version(
    tmp_path: pth.Path, profile_version: str
) -> None:
    """Old and superseded benchmark profile versions are incompatible."""
    payload = _plan_payload()
    payload["benchmark_profile_version"] = profile_version
    path = _write_json(tmp_path=tmp_path, filename="plan.json", payload=payload)

    with pytest.raises(IncompatibleBenchmarkProfileError, match="incompatible"):
        load_plan(path)


def test_load_plan_rejects_single_run_worker_iteration_metadata(
    tmp_path: pth.Path,
) -> None:
    """Legacy single-run worker-iteration metadata is incompatible."""
    payload = _plan_payload()
    payload.pop("worker_iterations")
    payload["worker_iteration"] = 1
    path = _write_json(tmp_path=tmp_path, filename="plan.json", payload=payload)

    with pytest.raises(IncompatibleBenchmarkProfileError, match="worker_iterations"):
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
