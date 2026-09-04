"""Shared pytest configuration and helpers for Cuprum unit tests.

The module imports hypothesis_crosshair_provider so Hypothesis can use the
CrossHair backend, registers the local ``crosshair`` Hypothesis profile, and
declares the matching pytest marker for symbolic helper-property checks.
"""

from __future__ import annotations

import typing as typ

import hypothesis_crosshair_provider  # ruff: ignore[unused-import]  # Registers CrossHair backend provider on import.
from hypothesis import settings

from benchmarks.benchmark_profile import BENCHMARK_PROFILE_VERSION
from benchmarks.ratchet_history import BaselineHistory, HistorySample

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

settings.register_profile(
    "crosshair",
    settings(backend="crosshair", deadline=None, derandomize=True, max_examples=50),
)

_VOLATILE_KEYS: frozenset[str] = frozenset({
    "sha256",
    "wall_time_seconds",
    "lock_wait_seconds",
    "output_bytes",
    "fixture_path",
    "wrapped_fixture_path",
    "output_dir",
    "profile_dir",
    "worker_command",
})

SCENARIO = "medium-single-nocb"
WORKER_ITERATIONS = 20
TYPICAL_RATIOS = (1.013, 1.001, 1.069, 0.916, 1.105)


def benchmark_run_payloads(
    ratios: cabc.Mapping[str, float], *, worker_iterations: int = 20
) -> tuple[dict[str, object], dict[str, object]]:
    """Build matching dry-run and Hyperfine payloads for scenario ratios.

    Parameters
    ----------
    ratios : collections.abc.Mapping[str, float]
        Comparison identifiers and their desired Rust-to-Python mean ratios.
    worker_iterations : int
        Positive worker count recorded in the generated plan; defaults to 20.

    Returns
    -------
    tuple[dict[str, object], dict[str, object]]
        Matching dry-run plan and Hyperfine throughput payloads.
    """
    scenarios: list[dict[str, object]] = []
    results: list[dict[str, object]] = []
    for scenario, ratio in sorted(ratios.items()):
        python_scenario: dict[str, object] = {
            "name": f"python-{scenario}",
            "backend": "python",
        }
        rust_scenario: dict[str, object] = {
            "name": f"rust-{scenario}",
            "backend": "rust",
        }
        scenarios.extend((python_scenario, rust_scenario))
        python_result: dict[str, object] = {
            "command": f"python-{scenario}",
            "mean": 1.0,
        }
        rust_result: dict[str, object] = {
            "command": f"rust-{scenario}",
            "mean": ratio,
        }
        results.extend((python_result, rust_result))
    return (
        {
            "benchmark_profile_version": BENCHMARK_PROFILE_VERSION,
            "worker_iterations": worker_iterations,
            "scenarios": scenarios,
        },
        {"results": results},
    )


def _sample(
    ratio: float,
    *,
    profile_version: str = BENCHMARK_PROFILE_VERSION,
    worker_iterations: int = WORKER_ITERATIONS,
    run_id: str = "1",
) -> HistorySample:
    """Return one configurable main-branch ratio sample."""
    return HistorySample(
        commit="0" * 40,
        run_id=run_id,
        benchmark_profile_version=profile_version,
        worker_iterations=worker_iterations,
        ratios={SCENARIO: ratio},
    )


def _history(*ratios: float) -> BaselineHistory:
    """Return an oldest-first baseline history for ``ratios``."""
    return BaselineHistory(
        samples=tuple(
            _sample(ratio, run_id=str(index)) for index, ratio in enumerate(ratios)
        )
    )


def redact(obj: object, keys: frozenset[str] = _VOLATILE_KEYS) -> object:
    """Recursively replace the values of nominated keys with '<redacted>'.

    Parameters
    ----------
    obj : object
        The recursively traversed value; only dictionaries and lists are
        copied recursively, while non-container values are returned
        unchanged.
    keys : frozenset[str]
        Dictionary-key names whose associated values are replaced with
        ``"<redacted>"``; defaults to ``_VOLATILE_KEYS``.

    Returns
    -------
    object
        A recursively copied dictionary or list with nominated keys redacted,
        or the original non-container value unchanged.
    """
    if isinstance(obj, dict):
        return {
            k: "<redacted>" if k in keys else redact(v, keys) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(item, keys) for item in obj]
    return obj


def pytest_configure(config: pytest.Config) -> None:
    """Register local pytest markers."""
    config.addinivalue_line(
        "markers",
        "crosshair: property tests suitable for Hypothesis' CrossHair backend",
    )
