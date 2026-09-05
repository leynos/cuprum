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
from benchmarks.ratchet_types import (
    BaselineReason,
    BaselineSource,
    ComparisonState,
    ConfirmationStatus,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

_BOOTSTRAP_SKIP_REASON = "no_previous_main_benchmark_baseline"
_DECISION_FIELDS = (
    "baseline_source",
    "baseline_reason",
    "compatible_sample_count",
    "comparison_state",
)


def _decision_values(
    report: cabc.Mapping[str, object],
) -> tuple[BaselineSource, BaselineReason, int, ComparisonState] | None:
    """Return validated decision fields when a report includes them."""
    values = [report.get(field) for field in _DECISION_FIELDS]
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        msg = "ratchet report must include every decision field"
        raise TypeError(msg)
    source, reason, sample_count, state = values
    if not isinstance(source, str) or not isinstance(reason, str):
        msg = "ratchet report has invalid decision fields"
        raise TypeError(msg)
    if isinstance(sample_count, bool) or not isinstance(sample_count, int):
        msg = "ratchet report has invalid decision fields"
        raise TypeError(msg)
    if not isinstance(state, str) or sample_count < 0:
        msg = "ratchet report has invalid decision fields"
        raise TypeError(msg)
    try:
        return (
            BaselineSource(source),
            BaselineReason(reason),
            sample_count,
            ComparisonState(state),
        )
    except ValueError as exc:
        msg = "ratchet report has unknown decision fields"
        raise ValueError(msg) from exc


def _confirmation_status(
    report: cabc.Mapping[str, object],
) -> ConfirmationStatus | None:
    """Return validated optional confirmation status from a ratchet report."""
    status = report.get("confirmation_status")
    if status is None:
        return None
    if not isinstance(status, str):
        msg = "ratchet report has invalid confirmation status"
        raise TypeError(msg)
    try:
        return ConfirmationStatus(status)
    except ValueError as exc:
        msg = "ratchet report has unknown confirmation status"
        raise ValueError(msg) from exc


def _decision_table(report: cabc.Mapping[str, object]) -> str:
    """Render optional durable ratchet-decision fields for workflow Markdown."""
    decision = _decision_values(report)
    if decision is None:
        return ""
    source, reason, sample_count, state = decision
    rows = [
        "| Ratchet decision | Value |\n| --- | --- |",
        f"| Baseline source | `{source}` |",
        f"| Baseline reason | `{reason}` |",
        f"| Compatible samples | {sample_count} |",
        f"| Comparison state | `{state}` |",
    ]
    confirmation = _confirmation_status(report)
    if confirmation is not None:
        rows.append(f"| Confirmation status | `{confirmation.value}` |")
    table = "\n".join(rows)
    return f"\n\n{table}"


def _ratchet_skip_detail(report: cabc.Mapping[str, object]) -> str:
    """Return the human-readable skip-reason string for a skipped ratchet run."""
    if report.get("reason") == _BOOTSTRAP_SKIP_REASON:
        detail = (
            "Rust regression ratchet skipped: no previous completed main "
            "baseline artefact."
        )
    else:
        detail = "Rust regression ratchet skipped."
    return detail + _decision_table(report)


def _ratchet_passed_status(report: cabc.Mapping[str, object]) -> RatchetStatus:
    """Return a passed or failed RatchetStatus based on the *passed* field."""
    passed_value = report.get("passed")
    if not isinstance(passed_value, bool):
        msg = "ratchet report must include a boolean passed field"
        raise TypeError(msg)
    detail = (
        "Rust regression ratchet passed."
        if passed_value
        else "Rust regression ratchet failed."
    )
    return RatchetStatus(
        status="passed" if passed_value else "failed",
        detail=detail + _decision_table(report),
    )


def load_ratchet_report(path: pth.Path) -> RatchetStatus:
    """Load the Rust regression ratchet report and summarize its status.

    Parameters
    ----------
    path : pathlib.Path
        Filesystem path to the ratchet-report JSON file to load.

    Returns
    -------
    RatchetStatus
        The ratchet status: ``skipped`` when no comparison was performed or no
        baseline was available, otherwise the passed or failed status.
    """
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
    """Render workflow-summary Markdown for the comparison report.

    Parameters
    ----------
    report : BenchmarkComparisonReport
        Comparison data whose rows and summary populate the rendered table.
    ratchet_status : RatchetStatus
        Workflow ratchet status rendered as the report's ratchet detail line.

    Returns
    -------
    str
        A Markdown document with a heading, the ratchet detail, and a table of
        per-scenario Python and Rust means, speed-up, and faster backend.
    """
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
