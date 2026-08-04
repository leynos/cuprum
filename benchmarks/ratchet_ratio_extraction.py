"""Extract within-run Rust/Python mean ratios for regression ratcheting.

This module groups matched benchmark scenarios by their backend-independent
comparison identifier, computes the Rust-to-Python mean ratio within each
benchmark run, and validates that baseline and candidate runs expose the same
comparison groups. Isolating this logic keeps the ratchet CLI module focused on
input loading, report writing, and orchestration.
"""

from __future__ import annotations

from benchmarks._validation import (
    _require_list,
    _require_mapping,
    _require_non_empty_string,
    _require_positive_float,
)


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

    result_command = _require_non_empty_string(
        result.get("command"), name=f"results[{index}].command"
    )
    if result_command != scenario_name:
        msg = (
            f"results[{index}].command {result_command!r} must match "
            f"scenarios[{index}].name {scenario_name!r}"
        )
        raise ValueError(msg)

    mean = _require_positive_float(result.get("mean"), name=f"results[{index}].mean")
    return comparison_id, backend, mean


def _collect_backend_means(
    scenarios: list[object],
    results: list[object],
) -> dict[str, dict[str, float]]:
    """Group mean runtimes by comparison identifier and backend."""
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
    """Map comparison identifiers to within-run Rust/Python mean ratios."""
    # Each ratio divides the Rust scenario mean by the matched Python
    # scenario mean from the same benchmark run, so runner speed and
    # interpreter startup overhead cancel out of the cross-run comparison.
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


def _validate_matching_comparison_groups(
    *,
    baseline_ratios: dict[str, float],
    candidate_ratios: dict[str, float],
) -> None:
    """Validate that baseline and candidate have the same comparison groups."""
    baseline_names = set(baseline_ratios)
    candidate_names = set(candidate_ratios)
    if baseline_names != candidate_names:
        missing_from_candidate = sorted(baseline_names - candidate_names)
        missing_from_baseline = sorted(candidate_names - baseline_names)
        msg = (
            "comparison groups must match across baseline and candidate runs: "
            f"missing_from_candidate={missing_from_candidate}, "
            f"missing_from_baseline={missing_from_baseline}"
        )
        raise ValueError(msg)
