"""Generate a Python-versus-Rust benchmark comparison report."""

from __future__ import annotations

import argparse
import json
import pathlib as pth
import sys

from benchmarks.comparison_analysis import (
    BenchmarkComparisonReport,
    BenchmarkComparisonRow,
    BenchmarkComparisonSummary,
    RatchetStatus,
    compare_candidate_backend_results,
)
from benchmarks.comparison_report import (
    load_ratchet_report,
    render_summary_markdown,
    write_report_json,
    write_summary_markdown,
)
from benchmarks.ratchet_rust_performance import load_plan, load_throughput

__all__ = [
    "BenchmarkComparisonReport",
    "BenchmarkComparisonRow",
    "BenchmarkComparisonSummary",
    "RatchetStatus",
    "compare_candidate_backend_results",
    "load_ratchet_report",
    "main",
    "render_summary_markdown",
    "write_report_json",
    "write_summary_markdown",
]


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
