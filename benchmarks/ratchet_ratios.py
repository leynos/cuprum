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
from benchmarks.ratchet_ratio_extraction import _extract_rust_python_ratios

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
    """Load and minimally validate dry-run plan JSON payload."""
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
    """Load and minimally validate hyperfine throughput JSON payload."""
    payload = _load_json(path)
    results = _require_list(payload.get("results"), name="results")

    for index, result_value in enumerate(results):
        result = _require_mapping(result_value, name=f"results[{index}]")
        _require_positive_float(result.get("mean"), name=f"results[{index}].mean")

    return payload


def _validate_backend(backend: str, *, index: int) -> None:
    """Raise ``ValueError`` when *backend* is not 'python' or 'rust'."""
    if backend not in {"python", "rust"}:
        msg = (
            f"scenarios[{index}].backend must be either 'python' or 'rust', "
            f"got {backend!r}"
        )
        raise ValueError(msg)


def _comparison_id_for_scenario(*, scenario_name: str, backend: str) -> str:
    """Return the backend-independent comparison identifier for one scenario."""
    prefix = f"{backend}-"
    if not scenario_name.startswith(prefix):
        msg = (
            f"scenario name {scenario_name!r} must start with expected backend "
            f"prefix {prefix!r}"
        )
        raise ValueError(msg)
    comparison_id = scenario_name.removeprefix(prefix)
    if not comparison_id:
        msg = f"scenario name {scenario_name!r} must include a comparison label"
        raise ValueError(msg)
    return comparison_id


def _extract_scenario_entry(
    *,
    index: int,
    scenario_value: object,
    result_value: object,
) -> tuple[str, str, float]:
    """Return ``(comparison_id, backend, mean)`` for one paired entry."""
    scenario = _require_mapping(scenario_value, name=f"scenarios[{index}]")
    result = _require_mapping(result_value, name=f"results[{index}]")

    backend = _require_non_empty_string(
        scenario.get("backend"), name=f"scenarios[{index}].backend"
    )
    scenario_name = _require_non_empty_string(
        scenario.get("name"), name=f"scenarios[{index}].name"
    )
    _validate_backend(backend, index=index)
    comparison_id = _comparison_id_for_scenario(
        scenario_name=scenario_name,
        backend=backend,
    )

    mean = _require_positive_float(result.get("mean"), name=f"results[{index}].mean")
    return comparison_id, backend, mean


def _collect_backend_means(
    scenarios: list[object],
    results: list[object],
) -> dict[str, dict[str, float]]:
    """Group mean runtimes by comparison identifier and backend.

    Raises ``ValueError`` when a comparison group contains duplicate entries
    for the same backend.
    """
    grouped: dict[str, dict[str, float]] = {}
    for index, (scenario_value, result_value) in enumerate(
        zip(scenarios, results, strict=True)
    ):
        comparison_id, backend, mean = _extract_scenario_entry(
            index=index,
            scenario_value=scenario_value,
            result_value=result_value,
        )
        group = grouped.setdefault(comparison_id, {})
        if backend in group:
            msg = (
                f"comparison group {comparison_id!r} contains duplicate "
                f"{backend!r} scenario entries"
            )
            raise ValueError(msg)
        group[backend] = mean
    return grouped


def _compute_group_ratio(
    *,
    context_name: str,
    comparison_id: str,
    group: dict[str, float],
) -> float:
    """Return the Rust/Python mean ratio for one comparison group."""
    python_mean = group.get("python")
    rust_mean = group.get("rust")
    if python_mean is None:
        msg = (
            f"{context_name}: comparison group {comparison_id!r} is missing "
            "its Python scenario"
        )
        raise ValueError(msg)
    if rust_mean is None:
        msg = (
            f"{context_name}: comparison group {comparison_id!r} is missing "
            "its Rust scenario"
        )
        raise ValueError(msg)
    return rust_mean / python_mean


def _build_ratio_map(
    *,
    context_name: str,
    grouped: dict[str, dict[str, float]],
) -> dict[str, float]:
    """Build a sorted comparison-id → Rust/Python ratio map."""
    ratios = {
        comparison_id: _compute_group_ratio(
            context_name=context_name,
            comparison_id=comparison_id,
            group=group,
        )
        for comparison_id, group in sorted(grouped.items())
    }
    if not ratios:
        msg = f"{context_name}: Rust scenarios are required for ratchet comparison"
        raise ValueError(msg)
    return ratios


def _extract_rust_python_ratios(
    *,
    plan_payload: dict[str, object],
    throughput_payload: dict[str, object],
    context_name: str,
) -> dict[str, float]:
    """Map comparison identifiers to within-run Rust/Python mean ratios.

    Each ratio divides the Rust scenario mean by the matched Python scenario
    mean from the same benchmark run, so runner speed and interpreter startup
    overhead cancel out of the cross-run comparison.
    """
    scenarios = _require_list(plan_payload.get("scenarios"), name="scenarios")
    results = _require_list(throughput_payload.get("results"), name="results")

    if len(scenarios) != len(results):
        msg = (
            f"{context_name}: plan scenario count ({len(scenarios)}) must match "
            f"throughput result count ({len(results)})"
        )
        raise ValueError(msg)

    grouped = _collect_backend_means(scenarios, results)

    return _build_ratio_map(context_name=context_name, grouped=grouped)


def run_ratios(payload: BenchmarkRunPayload) -> dict[str, float]:
    """Return the within-run Rust/Python ratios for one benchmark run.

    The public entry point the history recorder uses, so a recorded sample
    and a compared candidate are derived by the same code rather than by two
    implementations that agree until one is edited.
    """
    return _extract_rust_python_ratios(
        plan_payload=payload.plan,
        throughput_payload=payload.throughput,
        context_name=payload.context_name,
    )


def profile_metadata(plan: dict[str, object]) -> tuple[str, int]:
    """Return the validated ``(profile version, worker iterations)`` of a plan."""
    validate_profile_version(plan)
    try:
        worker_iterations = require_worker_iterations(plan)
    except (TypeError, ValueError) as exc:
        raise IncompatibleBenchmarkProfileError(str(exc)) from exc
    return BENCHMARK_PROFILE_VERSION, worker_iterations
