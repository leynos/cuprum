"""Benchmark profile metadata shared by throughput and ratchet helpers."""

from __future__ import annotations

import json
import typing as typ

BENCHMARK_PROFILE_VERSION = "pipeline-worker-batched-v1"

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth


class IncompatibleBenchmarkProfileError(ValueError):
    """Raised when benchmark profiles are not comparable."""


def require_worker_iterations(payload: cabc.Mapping[str, object]) -> int:
    """Return the benchmark worker iteration count from a dry-run plan."""
    value = payload.get("worker_iterations")
    if isinstance(value, bool) or not isinstance(value, int):
        msg = "worker_iterations must be an int"
        raise TypeError(msg)
    if value < 1:
        msg = "worker_iterations must be >= 1"
        raise ValueError(msg)
    return value


def validate_profile_version(payload: cabc.Mapping[str, object]) -> None:
    """Validate that a benchmark plan uses the current profile version."""
    profile_value = payload.get("benchmark_profile_version")
    if not isinstance(profile_value, str) or not profile_value.strip():
        msg = "benchmark_profile_version is missing from benchmark plan"
        raise IncompatibleBenchmarkProfileError(msg)
    if profile_value != BENCHMARK_PROFILE_VERSION:
        msg = (
            "benchmark_profile_version is incompatible: "
            f"expected {BENCHMARK_PROFILE_VERSION!r}, got {profile_value!r}"
        )
        raise IncompatibleBenchmarkProfileError(msg)


def validate_matching_profiles(
    *,
    baseline_plan: cabc.Mapping[str, object],
    candidate_plan: cabc.Mapping[str, object],
) -> None:
    """Validate that benchmark profile metadata matches across both plans."""
    baseline_iterations = baseline_plan.get("worker_iterations")
    candidate_iterations = candidate_plan.get("worker_iterations")
    if baseline_iterations != candidate_iterations:
        msg = (
            "worker_iterations must match across baseline and candidate runs: "
            f"baseline={baseline_iterations!r}, candidate={candidate_iterations!r}"
        )
        raise IncompatibleBenchmarkProfileError(msg)


def write_incompatible_profile_report(*, reason: str, output_path: pth.Path) -> None:
    """Write a successful skip report when baseline data is not comparable."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "baseline_available": True,
        "comparison_performed": False,
        "passed": True,
        "reason": "incompatible_benchmark_profile",
        "detail": reason,
    }
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
