"""Report rendering for Python-versus-Rust benchmark comparisons."""

from __future__ import annotations

import json
import typing as typ

from benchmarks._validation import _require_mapping
from benchmarks.benchmark_workload import (
    CI_RATCHET_WORKLOAD,
    SMOKE_WORKLOAD,
    THROUGHPUT_SWEEP_WORKLOAD,
    WorkloadProtocol,
)
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

    from benchmarks.benchmark_workload import WorkloadName

_BOOTSTRAP_SKIP_REASON = "no_previous_main_benchmark_baseline"
_DECISION_FIELDS = (
    "baseline_source",
    "baseline_reason",
    "compatible_sample_count",
    "comparison_state",
)

#: Rendered workload descriptions, keyed by workload identifier. This is report
#: prose, so it lives with the report rather than with the protocol value the
#: plan recorded — wording changes here cannot reach what a run measured. The
#: ``WorkloadName`` key type is what makes the lookup total: a protocol cannot
#: be constructed carrying a workload absent from this table, so rendering one
#: cannot fail.
_WORKLOAD_DESCRIPTIONS: dict[WorkloadName, str] = {
    THROUGHPUT_SWEEP_WORKLOAD: "the throughput sweep, covering three payload tiers",
    SMOKE_WORKLOAD: "the smoke workload, the sweep's shape at reduced payloads",
    CI_RATCHET_WORKLOAD: (
        "the CI-ratchet workload, one large payload measured at the ratchet's "
        "own worker-iteration count"
    ),
}


def _complete_decision_fields(
    report: cabc.Mapping[str, object],
) -> tuple[object, object, object, object] | None:
    """Return all decision fields or reject an incomplete decision record."""
    values = tuple(report.get(field) for field in _DECISION_FIELDS)
    if values == (None, None, None, None):
        return None
    if any(value is None for value in values):
        msg = "ratchet report must include every decision field"
        raise TypeError(msg)
    source, reason, sample_count, state = values
    return source, reason, sample_count, state


def _decision_string(value: object) -> str:
    """Validate one string-valued decision field."""
    if not isinstance(value, str):
        msg = "ratchet report has invalid decision fields"
        raise TypeError(msg)
    return value


def _compatible_sample_count(value: object) -> int:
    """Validate one non-negative compatible-sample count."""
    if isinstance(value, bool) or not isinstance(value, int):
        msg = "ratchet report has invalid decision fields"
        raise TypeError(msg)
    if value < 0:
        msg = "ratchet report has invalid decision fields"
        raise ValueError(msg)
    return value


def _decision_enums(
    *,
    source: str,
    reason: str,
    state: str,
) -> tuple[BaselineSource, BaselineReason, ComparisonState]:
    """Convert validated decision strings to bounded enum values."""
    try:
        return (
            BaselineSource(source),
            BaselineReason(reason),
            ComparisonState(state),
        )
    except ValueError as exc:
        msg = "ratchet report has unknown decision fields"
        raise ValueError(msg) from exc


def _decision_values(
    report: cabc.Mapping[str, object],
) -> tuple[BaselineSource, BaselineReason, int, ComparisonState] | None:
    """Return validated decision fields when a report includes them."""
    values = _complete_decision_fields(report)
    if values is None:
        return None
    raw_source, raw_reason, raw_count, raw_state = values
    source, reason, state = _decision_enums(
        source=_decision_string(raw_source),
        reason=_decision_string(raw_reason),
        state=_decision_string(raw_state),
    )
    return source, reason, _compatible_sample_count(raw_count), state


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


def describe_protocol(protocol: WorkloadProtocol) -> str:
    """Return a one-line summary of a workload and the protocol it recorded.

    Only the metadata the plan actually carried is named. A plan that omits a
    field is summarized without it rather than with a default, because a
    default here would state a measurement protocol as fact when nothing
    recorded it.

    Parameters
    ----------
    protocol : WorkloadProtocol
        The validated protocol a plan described.

    Returns
    -------
    str
        The summary rendered into maintainer-facing report prose.

    Examples
    --------
    >>> describe_protocol(
    ...     WorkloadProtocol(
    ...         workload="ci-ratchet",
    ...         profile_version=None,
    ...         worker_iterations=5,
    ...         payload_bytes=(1024,),
    ...     )
    ... )
    'the ci-ratchet workload, payload 0 MiB, 5 worker iterations'
    """
    parts = [f"the {protocol.workload} workload"]
    if protocol.profile_version is not None:
        parts.append(f"profile {protocol.profile_version}")
    if protocol.payload_bytes:
        sizes = "/".join(
            f"{size / (1024 * 1024):.0f}" for size in protocol.payload_bytes
        )
        parts.append(
            f"payload {sizes} MiB"
            if len(protocol.payload_bytes) == 1
            else f"payloads {sizes} MiB"
        )
    if protocol.worker_iterations is not None:
        parts.append(f"{protocol.worker_iterations} worker iterations")
    return ", ".join(parts)


def describe_workload(protocol: WorkloadProtocol) -> str:
    """Return the prose sentence naming the protocol's workload.

    Parameters
    ----------
    protocol : WorkloadProtocol
        The validated protocol a plan described.

    Returns
    -------
    str
        The workload's expanded description, as rendered into report prose.

    Examples
    --------
    >>> describe_workload(
    ...     WorkloadProtocol(
    ...         workload="smoke",
    ...         profile_version=None,
    ...         worker_iterations=None,
    ...         payload_bytes=(),
    ...     )
    ... )
    "the smoke workload, the sweep's shape at reduced payloads"
    """
    return _WORKLOAD_DESCRIPTIONS[protocol.workload]


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
        A Markdown document with a heading, the workload and protocol the
        compared plan recorded, the ratchet detail, and a table of
        per-scenario Python and Rust means, speed-up, and faster backend.
    """
    lines = [
        "## Python vs Rust benchmark comparison",
        "",
        (
            "Candidate results for the current workflow run, measured on "
            f"{describe_protocol(report.protocol)}."
        ),
        "",
        (
            f"The compared scenarios are {describe_workload(report.protocol)}, "
            "which is what the ratchet compares between runs; a report for a "
            "different workload does not describe the ratchet's own measurement."
        ),
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
