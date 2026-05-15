"""Report rendering for Python-versus-Rust benchmark comparisons."""

from __future__ import annotations

import json
import typing as typ

from benchmarks._validation import _require_mapping
from benchmarks.comparison_analysis import (
    BenchmarkComparisonReport,
    RatchetStatus,
    _require_optional_bool,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

_BOOTSTRAP_SKIP_REASON = "no_previous_main_benchmark_baseline"


def _ratchet_skip_detail(report: cabc.Mapping[str, object]) -> str:
    """Return the human-readable skip-reason string for a skipped ratchet run."""
    if report.get("reason") == _BOOTSTRAP_SKIP_REASON:
        return (
            "Rust regression ratchet skipped: no previous successful main "
            "baseline artefact."
        )
    return "Rust regression ratchet skipped."


def _ratchet_passed_status(report: cabc.Mapping[str, object]) -> RatchetStatus:
    """Return a passed or failed RatchetStatus based on the *passed* field."""
    passed_value = report.get("passed")
    if not isinstance(passed_value, bool):
        msg = "ratchet report must include a boolean passed field"
        raise TypeError(msg)
    if passed_value:
        return RatchetStatus(status="passed", detail="Rust regression ratchet passed.")
    return RatchetStatus(status="failed", detail="Rust regression ratchet failed.")


def load_ratchet_report(path: pth.Path) -> RatchetStatus:
    """Load the Rust regression ratchet report and summarize its status."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    report = _require_mapping(payload, name=f"ratchet report from {path}")

    comparison_performed = _require_optional_bool(report, "comparison_performed", path)
    baseline_available = _require_optional_bool(report, "baseline_available", path)

    if comparison_performed is False or baseline_available is False:
        return RatchetStatus(status="skipped", detail=_ratchet_skip_detail(report))

    return _ratchet_passed_status(report)


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
