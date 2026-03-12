"""Generate a Python-versus-Rust benchmark comparison report.

This module reads the filtered candidate benchmark plan plus throughput JSON
produced by the benchmark-ratchet workflow, pairs Python and Rust results for
the same scenario label, writes a structured JSON report, and renders Markdown
summary content suitable for ``$GITHUB_STEP_SUMMARY``.
"""

from __future__ import annotations

import argparse
import dataclasses as dc
import json
import pathlib as pth
import sys

from benchmarks._validation import (
    _require_list,
    _require_mapping,
    _require_non_empty_string,
)
from benchmarks.ratchet_rust_performance import (
    _require_positive_float,
    load_plan,
    load_throughput,
)

_FLOAT_TOLERANCE = 1e-12
_BOOTSTRAP_SKIP_REASON = "no_previous_main_benchmark_baseline"


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


def _build_report_from_grouped_entries(
    grouped: dict[str, dict[str, _ScenarioEntry]],
) -> BenchmarkComparisonReport:
    """Build the final report from grouped backend entries."""
    rows: list[BenchmarkComparisonRow] = []
    rust_wins = 0
    python_wins = 0
    ties = 0
    for comparison_id in sorted(grouped):
        entries = grouped[comparison_id]
        python_entry = entries.get("python")
        if python_entry is None:
            msg = f"comparison group {comparison_id!r} is missing Python scenario"
            raise ValueError(msg)
        rust_entry = entries.get("rust")
        if rust_entry is None:
            msg = f"comparison group {comparison_id!r} is missing Rust scenario"
            raise ValueError(msg)
        row = _build_row(
            comparison_id=comparison_id,
            python_entry=python_entry,
            rust_entry=rust_entry,
        )
        rows.append(row)
        if row.faster_backend == "rust":
            rust_wins += 1
        elif row.faster_backend == "python":
            python_wins += 1
        else:
            ties += 1

    return BenchmarkComparisonReport(
        rows=tuple(rows),
        summary=BenchmarkComparisonSummary(
            row_count=len(rows),
            rust_wins=rust_wins,
            python_wins=python_wins,
            ties=ties,
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


def load_ratchet_report(path: pth.Path) -> RatchetStatus:
    """Load the Rust regression ratchet report and summarize its status."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    report = _require_mapping(payload, name=f"ratchet report from {path}")

    comparison_performed = report.get("comparison_performed")
    baseline_available = report.get("baseline_available")
    if comparison_performed is False or baseline_available is False:
        reason = report.get("reason")
        if reason == _BOOTSTRAP_SKIP_REASON:
            detail = (
                "Rust regression ratchet skipped: no previous successful main "
                "baseline artefact."
            )
        else:
            detail = "Rust regression ratchet skipped."
        return RatchetStatus(status="skipped", detail=detail)

    passed_value = report.get("passed")
    if not isinstance(passed_value, bool):
        msg = "ratchet report must include a boolean passed field"
        raise TypeError(msg)
    if passed_value:
        return RatchetStatus(status="passed", detail="Rust regression ratchet passed.")
    return RatchetStatus(status="failed", detail="Rust regression ratchet failed.")


def render_summary_markdown(
    *,
    report: BenchmarkComparisonReport,
    ratchet_status: RatchetStatus,
) -> str:
    """Render workflow-summary Markdown for the comparison report."""
    lines = [
        "## Python vs Rust benchmark comparison",
        "",
        "Candidate smoke benchmark results for the current workflow run.",
        "",
        ratchet_status.detail,
        "",
        "| Scenario | Python mean (s) | Rust mean (s) | Speedup | Faster backend |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    lines.extend(
        (
            f"| `{row.comparison_id}` | {row.python_mean:.6f} | "
            f"{row.rust_mean:.6f} | {row.speedup_ratio:.2f}x | "
            f"{row.faster_backend} |"
        )
        for row in report.rows
    )
    return "\n".join(lines) + "\n"


def write_report_json(
    *,
    report: BenchmarkComparisonReport,
    ratchet_status: RatchetStatus,
    output_path: pth.Path,
) -> None:
    """Write the structured JSON comparison report."""
    payload = report.as_dict()
    payload["ratchet_status"] = ratchet_status.as_dict()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def write_summary_markdown(*, markdown: str, output_path: pth.Path) -> None:
    """Write the Markdown summary file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the comparison-report CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=pth.Path, required=True)
    parser.add_argument("--throughput", type=pth.Path, required=True)
    parser.add_argument("--ratchet-report", type=pth.Path, required=True)
    parser.add_argument("--output-json", type=pth.Path, required=True)
    parser.add_argument("--output-markdown", type=pth.Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Generate the benchmark comparison report and workflow summary markdown."""
    args = _parse_args(argv)
    try:
        report = compare_candidate_backend_results(
            plan_payload=load_plan(args.plan),
            throughput_payload=load_throughput(args.throughput),
        )
        ratchet_status = load_ratchet_report(args.ratchet_report)
        markdown = render_summary_markdown(
            report=report,
            ratchet_status=ratchet_status,
        )
        write_report_json(
            report=report,
            ratchet_status=ratchet_status,
            output_path=args.output_json,
        )
        write_summary_markdown(markdown=markdown, output_path=args.output_markdown)
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        print(
            f"benchmark comparison report failed to evaluate inputs: {exc}",
            file=sys.stderr,
        )
        return 2

    print(
        "benchmark comparison report generated: "
        f"{report.summary.row_count} paired scenario rows",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
