"""Benchmark profile metadata shared by throughput and ratchet helpers."""

from __future__ import annotations

import json
import logging
import typing as typ

_logger = logging.getLogger(__name__)

# v3: the CI ratchet pairs matched Python/Rust commands and measures ten runs,
# so baselines collected with the noisier v2 command order are not comparable.
BENCHMARK_PROFILE_VERSION = "pipeline-worker-release-ratio-v3"

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
    _require_profile_version(payload, context_name=None)


def _require_profile_version(
    payload: cabc.Mapping[str, object],
    *,
    context_name: str | None,
) -> str:
    """Return validated benchmark profile version metadata."""
    profile_value = payload.get("benchmark_profile_version")
    if not isinstance(profile_value, str) or not profile_value.strip():
        msg = (
            f"{context_name} benchmark_profile_version is missing"
            if context_name is not None
            else "benchmark_profile_version is missing from benchmark plan"
        )
        _logger.warning("benchmark profile incompatible: %s", msg)
        raise IncompatibleBenchmarkProfileError(msg)
    if profile_value != BENCHMARK_PROFILE_VERSION:
        prefix = f"{context_name} " if context_name is not None else ""
        msg = (
            f"{prefix}benchmark_profile_version is incompatible: "
            f"expected {BENCHMARK_PROFILE_VERSION!r}, got {profile_value!r}"
        )
        _logger.warning("benchmark profile incompatible: %s", msg)
        raise IncompatibleBenchmarkProfileError(msg)
    return profile_value


def validate_matching_profiles(
    *,
    baseline_plan: cabc.Mapping[str, object],
    candidate_plan: cabc.Mapping[str, object],
) -> None:
    """Validate that benchmark profile metadata matches across both plans."""
    baseline_profile = _require_profile_metadata(baseline_plan, context_name="baseline")
    candidate_profile = _require_profile_metadata(
        candidate_plan, context_name="candidate"
    )
    baseline_version, baseline_iterations = baseline_profile
    candidate_version, candidate_iterations = candidate_profile
    if baseline_version != candidate_version:
        msg = (
            "benchmark_profile_version must match across baseline and candidate "
            f"runs: baseline={baseline_version!r}, candidate={candidate_version!r}"
        )
        _logger.warning("benchmark profile mismatch: %s", msg)
        raise IncompatibleBenchmarkProfileError(msg)
    if baseline_iterations != candidate_iterations:
        msg = (
            "worker_iterations must match across baseline and candidate runs: "
            f"baseline={baseline_iterations!r}, candidate={candidate_iterations!r}"
        )
        _logger.warning("benchmark profile mismatch: %s", msg)
        raise IncompatibleBenchmarkProfileError(msg)


def _require_profile_metadata(
    payload: cabc.Mapping[str, object],
    *,
    context_name: str,
) -> tuple[str, int]:
    """Return validated profile metadata for ``validate_matching_profiles``."""
    profile_value = _require_profile_version(payload, context_name=context_name)
    try:
        worker_iterations = require_worker_iterations(payload)
    except (TypeError, ValueError) as exc:
        msg = f"{context_name} {exc}"
        _logger.warning("benchmark profile metadata error: %s", msg)
        raise IncompatibleBenchmarkProfileError(msg) from exc
    return profile_value, worker_iterations


def write_incompatible_profile_report(*, reason: str, output_path: pth.Path) -> None:
    """Write a successful skip report when baseline data is not comparable."""
    _logger.debug(
        "writing incompatible profile report: path=%s, reason=%r",
        output_path,
        reason,
    )
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
