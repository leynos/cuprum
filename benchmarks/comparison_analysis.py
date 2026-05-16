"""Python-versus-Rust benchmark comparison analysis."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import typing as typ
from collections import Counter

from benchmarks._validation import (
    _require_list,
    _require_mapping,
    _require_non_empty_string,
)
from benchmarks.ratchet_rust_performance import _require_positive_float

if typ.TYPE_CHECKING:
    import pathlib as pth

_FLOAT_TOLERANCE = 1e-12


@dc.dataclass(frozen=True, slots=True)
class BenchmarkComparisonRow:
    """One matched Python-versus-Rust benchmark comparison row."""

    comparison_id: str
    python_scenario_name: str
    rust_scenario_name: str
    python_mean: float
    rust_mean: float
    speedup_ratio: float
    faster_backend: str

    def as_dict(self) -> dict[str, object]:
        """Serialize the row for JSON output."""
        return {
            "comparison_id": self.comparison_id,
            "python_scenario_name": self.python_scenario_name,
            "rust_scenario_name": self.rust_scenario_name,
            "python_mean": self.python_mean,
            "rust_mean": self.rust_mean,
            "speedup_ratio": self.speedup_ratio,
            "faster_backend": self.faster_backend,
        }


@dc.dataclass(frozen=True, slots=True)
class BenchmarkComparisonSummary:
    """Aggregate counts for the comparison report."""

    row_count: int
    rust_wins: int
    python_wins: int
    ties: int

    def as_dict(self) -> dict[str, object]:
        """Serialize the summary for JSON output."""
        return {
            "row_count": self.row_count,
            "rust_wins": self.rust_wins,
            "python_wins": self.python_wins,
            "ties": self.ties,
        }


@dc.dataclass(frozen=True, slots=True)
class BenchmarkComparisonReport:
    """Structured comparison report for candidate benchmark results."""

    rows: tuple[BenchmarkComparisonRow, ...]
    summary: BenchmarkComparisonSummary

    def as_dict(self) -> dict[str, object]:
        """Serialize the report for JSON output."""
        return {
            "rows": [row.as_dict() for row in self.rows],
            "summary": self.summary.as_dict(),
        }


@dc.dataclass(frozen=True, slots=True)
class RatchetStatus:
    """Workflow-friendly summary of the Rust regression ratchet state."""

    status: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        """Serialize the ratchet status for JSON output."""
        return {
            "status": self.status,
            "detail": self.detail,
        }


@dc.dataclass(frozen=True, slots=True)
class _ScenarioEntry:
    """Validated scenario result entry used for pairing backends."""

    scenario_name: str
    mean: float


def _validate_backend(backend: str, *, index: int) -> None:
    """Validate that *backend* is one of the supported benchmark backends."""
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


def _build_row(
    *,
    comparison_id: str,
    python_entry: _ScenarioEntry,
    rust_entry: _ScenarioEntry,
) -> BenchmarkComparisonRow:
    """Build one comparison row from paired Python and Rust entries."""
    speedup_ratio = python_entry.mean / rust_entry.mean
    if abs(python_entry.mean - rust_entry.mean) <= _FLOAT_TOLERANCE:
        faster_backend = "tie"
    elif rust_entry.mean < python_entry.mean:
        faster_backend = "rust"
    else:
        faster_backend = "python"
    return BenchmarkComparisonRow(
        comparison_id=comparison_id,
        python_scenario_name=python_entry.scenario_name,
        rust_scenario_name=rust_entry.scenario_name,
        python_mean=python_entry.mean,
        rust_mean=rust_entry.mean,
        speedup_ratio=speedup_ratio,
        faster_backend=faster_backend,
    )


def _extract_candidate_entry(
    *,
    index: int,
    scenario_value: object,
    result_value: object,
) -> tuple[str, str, _ScenarioEntry]:
    """Extract one validated backend entry for candidate comparison."""
    scenario = _require_mapping(scenario_value, name=f"scenarios[{index}]")
    result = _require_mapping(result_value, name=f"results[{index}]")
    backend = _require_non_empty_string(
        scenario.get("backend"), name=f"scenarios[{index}].backend"
    )
    _validate_backend(backend, index=index)
    scenario_name = _require_non_empty_string(
        scenario.get("name"), name=f"scenarios[{index}].name"
    )
    comparison_id = _comparison_id_for_scenario(
        scenario_name=scenario_name,
        backend=backend,
    )
    mean = _require_positive_float(result.get("mean"), name=f"results[{index}].mean")
    return (
        comparison_id,
        backend,
        _ScenarioEntry(scenario_name=scenario_name, mean=mean),
    )


def _get_required_entries(
    comparison_id: str,
    entries: dict[str, _ScenarioEntry],
) -> tuple[_ScenarioEntry, _ScenarioEntry]:
    """Return (python_entry, rust_entry) or raise ValueError if either is absent."""
    python_entry = entries.get("python")
    if python_entry is None:
        msg = f"comparison group {comparison_id!r} is missing Python scenario"
        raise ValueError(msg)
    rust_entry = entries.get("rust")
    if rust_entry is None:
        msg = f"comparison group {comparison_id!r} is missing Rust scenario"
        raise ValueError(msg)
    return python_entry, rust_entry


def _build_report_from_grouped_entries(
    grouped: dict[str, dict[str, _ScenarioEntry]],
) -> BenchmarkComparisonReport:
    """Build the final report from grouped backend entries."""
    rows: list[BenchmarkComparisonRow] = []
    tally: Counter[str] = Counter()
    for comparison_id in sorted(grouped):
        python_entry, rust_entry = _get_required_entries(
            comparison_id, grouped[comparison_id]
        )
        row = _build_row(
            comparison_id=comparison_id,
            python_entry=python_entry,
            rust_entry=rust_entry,
        )
        rows.append(row)
        tally[row.faster_backend] += 1

    return BenchmarkComparisonReport(
        rows=tuple(rows),
        summary=BenchmarkComparisonSummary(
            row_count=len(rows),
            rust_wins=tally["rust"],
            python_wins=tally["python"],
            ties=tally["tie"],
        ),
    )


def compare_candidate_backend_results(
    *,
    plan_payload: dict[str, object],
    throughput_payload: dict[str, object],
) -> BenchmarkComparisonReport:
    """Compare matched Python and Rust candidate benchmark results."""
    scenarios = _require_list(plan_payload.get("scenarios"), name="scenarios")
    results = _require_list(throughput_payload.get("results"), name="results")
    if len(scenarios) != len(results):
        msg = (
            f"plan scenario count ({len(scenarios)}) must match throughput "
            f"result count ({len(results)})"
        )
        raise ValueError(msg)

    grouped: dict[str, dict[str, _ScenarioEntry]] = {}
    for index, (scenario_value, result_value) in enumerate(
        zip(scenarios, results, strict=True)
    ):
        comparison_id, backend, entry = _extract_candidate_entry(
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
        group[backend] = entry

    return _build_report_from_grouped_entries(grouped)


def _require_optional_bool(
    report: cabc.Mapping[str, object],
    field: str,
    path: pth.Path,
) -> bool | None:
    """Return the boolean value of *field* from *report*, or None if absent."""
    value = report.get(field)
    if value is not None and not isinstance(value, bool):
        msg = f"ratchet report from {path} has non-boolean {field!r} field: {value!r}"
        raise TypeError(msg)
    return value
