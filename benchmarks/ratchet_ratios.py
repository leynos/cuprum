"""Turn benchmark plan and throughput JSON into within-run Rust/Python ratios.

The ratchet compares ratios, not wall-clock means: dividing a run's Rust
scenario mean by its matched Python mean cancels the runner speed and
interpreter startup overhead the two runs do not share. Extracting them is
mechanical and fiddly — pairing scenarios with results by position, matching
backends by name prefix, rejecting a group that lost half of its pair — so it
lives here, apart from the policy that judges the numbers.

Both the comparison and the main-branch history recorder read runs through
`run_ratios`, so a recorded sample and a compared candidate are derived by
the same code rather than by two implementations that agree until one is
edited.
"""

from __future__ import annotations

import json
import logging
import typing as typ

from benchmarks._validation import (
    _require_list,
    _require_mapping,
    _require_non_empty_string,
    _require_positive_float,
)
from benchmarks.benchmark_profile import (
    BENCHMARK_PROFILE_VERSION,
    IncompatibleBenchmarkProfileError,
    require_worker_iterations,
    validate_profile_version,
)
from benchmarks.ratchet_ratio_extraction import (
    _extract_rust_python_ratios as _extract_validated_rust_python_ratios,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

    from benchmarks.ratchet_types import BenchmarkRunPayload

_logger = logging.getLogger(__name__)


def _load_json(path: pth.Path) -> dict[str, object]:
    """Load a JSON object payload from ``path``."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        msg = f"expected a JSON object in {path}, got {type(payload).__name__}"
        raise TypeError(msg)
    return typ.cast("dict[str, object]", payload)


def load_plan(path: pth.Path) -> dict[str, object]:
    """Load and minimally validate a dry-run plan JSON payload.

    Parameters
    ----------
    path : pathlib.Path
        Plan file written by the benchmark profile command.

    Returns
    -------
    dict[str, object]
        Validated plan payload.

    Raises
    ------
    OSError, json.JSONDecodeError, TypeError, ValueError
        If the file cannot be read, is not a JSON object, or lacks required
        profile and scenario fields.
    IncompatibleBenchmarkProfileError
        If its worker-iteration metadata is incompatible.
    """  # ruff: ignore[docstring-extraneous-exception] - validation and I/O errors propagate from dedicated helpers.
    _logger.debug("loading benchmark plan: path=%s", path)
    payload = _load_json(path)
    validate_profile_version(payload)
    try:
        require_worker_iterations(payload)
    except (TypeError, ValueError) as exc:
        _logger.warning("benchmark plan worker_iterations invalid: %s", exc)
        raise IncompatibleBenchmarkProfileError(str(exc)) from exc
    scenarios = _require_list(payload.get("scenarios"), name="scenarios")

    for index, scenario_value in enumerate(scenarios):
        scenario = _require_mapping(
            scenario_value,
            name=f"scenarios[{index}]",
        )
        _require_non_empty_string(
            scenario.get("name"),
            name=f"scenarios[{index}].name",
        )
        _require_non_empty_string(
            scenario.get("backend"),
            name=f"scenarios[{index}].backend",
        )

    return payload


def load_throughput(path: pth.Path) -> dict[str, object]:
    """Load and minimally validate a Hyperfine throughput JSON payload.

    Parameters
    ----------
    path : pathlib.Path
        Throughput file written by Hyperfine.

    Returns
    -------
    dict[str, object]
        Validated throughput payload.

    Raises
    ------
    OSError, json.JSONDecodeError, TypeError, ValueError
        If the file cannot be read, is not a JSON object, or has invalid
        result means.
    """  # ruff: ignore[docstring-extraneous-exception] - validation and I/O errors propagate from dedicated helpers.
    payload = _load_json(path)
    results = _require_list(payload.get("results"), name="results")

    for index, result_value in enumerate(results):
        result = _require_mapping(result_value, name=f"results[{index}]")
        _require_positive_float(result.get("mean"), name=f"results[{index}].mean")

    return payload


def run_ratios(payload: BenchmarkRunPayload) -> dict[str, float]:
    """Return the within-run Rust/Python ratios for one benchmark run.

    The public entry point the history recorder uses, so a recorded sample
    and a compared candidate are derived by the same code rather than by two
    implementations that agree until one is edited.

    Parameters
    ----------
    payload : BenchmarkRunPayload
        One run's plan and throughput payloads, whose paired Python and Rust
        scenarios must be aligned and valid, plus context for error messages.

    Returns
    -------
    dict[str, float]
        Each comparison identifier's Rust-to-Python mean ratio.

    Raises
    ------
    TypeError, ValueError
        If the payloads do not describe aligned, valid Python and Rust pairs.
    """  # ruff: ignore[docstring-extraneous-exception] - extraction keeps the malformed-payload errors intact.
    return _extract_validated_rust_python_ratios(
        plan_payload=payload.plan,
        throughput_payload=payload.throughput,
        context_name=payload.context_name,
    )


def profile_metadata(plan: dict[str, object]) -> tuple[str, int]:
    """Return the validated ``(profile version, worker iterations)`` of a plan.

    Parameters
    ----------
    plan : dict[str, object]
        Validated benchmark plan payload.

    Returns
    -------
    tuple[str, int]
        Current profile version and positive worker-iteration count.

    Raises
    ------
    IncompatibleBenchmarkProfileError
        If worker-iteration metadata is absent or invalid.
    ValueError
        If the profile version is incompatible.
    """  # ruff: ignore[docstring-extraneous-exception] - profile validation deliberately propagates its contract error.
    validate_profile_version(plan)
    try:
        worker_iterations = require_worker_iterations(plan)
    except (TypeError, ValueError) as exc:
        raise IncompatibleBenchmarkProfileError(str(exc)) from exc
    return BENCHMARK_PROFILE_VERSION, worker_iterations
