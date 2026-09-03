"""Execute and parse the benchmark-gate summary script declared by CI."""

from __future__ import annotations

import dataclasses as dc
import subprocess  # ruff: ignore[suspicious-subprocess-import] - tests run the checked-in workflow script.
import typing as typ

from tests.helpers.workflow import (
    CHANGES_JOB,
    script_of,
    step_named,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

    from tests.helpers.workflow import Workflow

SUMMARY_STEP = "Record the benchmark gate decision"
_COLUMNS = (
    "event",
    "detector",
    "performance-relevant changes",
    "benchmark-ratchet",
)
_FIELD_NAMES = ("event", "detector", "bench", "decision")
_METRIC_PREFIX = "::notice title=benchmark-gate-decision::"


@dc.dataclass(frozen=True, slots=True)
class Detector:
    """Represent the paths-filter result supplied to the summary step.

    Attributes
    ----------
    outcome : str
        Closed-set outcome reported by the detector step.
    bench : str
        Changed-path verdict emitted by the detector, or an empty string when
        no verdict was produced.
    """

    outcome: str
    bench: str


@dc.dataclass(frozen=True, slots=True)
class Summary:
    """Represent the parsed row emitted by the summary script.

    Attributes
    ----------
    fields : dict[str, str]
        Summary columns keyed by their names in the workflow table.
    table : str
        Canonical Markdown table emitted by the workflow summary script.
    metric : dict[str, str]
        Bounded labels emitted in the workflow annotation.
    """

    fields: dict[str, str]
    table: str
    metric: dict[str, str]


@dc.dataclass(frozen=True, slots=True)
class SummaryCase:
    """Describe one event and detector combination for summary validation.

    Attributes
    ----------
    event : str
        Event supplied to the workflow summary script.
    detector : Detector
        Detector result supplied to the workflow summary script.
    decision : str
        Expected bounded benchmark decision.
    """

    event: str
    detector: Detector
    decision: str


def _summary_script(workflow_data: Workflow) -> str:
    """Return the summary step's script, as `ci.yml` declares it."""
    script = script_of(step_named(workflow_data, CHANGES_JOB, SUMMARY_STEP))
    assert script is not None, f"the {SUMMARY_STEP!r} step must run a script"
    return script


def _execute_summary_script(
    *,
    event: str,
    detector: Detector,
    summary_path: pth.Path,
    workflow_data: Workflow,
) -> subprocess.CompletedProcess[str]:
    """Execute the checked-in summary script."""
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - literal vector plus workflow run block; no test input reaches the command line.
        ["/usr/bin/env", "bash", "-c", _summary_script(workflow_data)],
        env={
            "PATH": "/usr/bin:/bin",
            "EVENT": event,
            "BENCH": detector.bench,
            "DETECTOR": detector.outcome,
            "GITHUB_STEP_SUMMARY": str(summary_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        f"the summary script must not fail; stderr was:\n{completed.stderr}"
    )
    return completed


def _parse_summary(*, emitted: str, stdout: str) -> Summary:
    """Parse the summary table and workflow annotation."""
    rows = [
        line
        for line in emitted.splitlines()
        if line.startswith("|") and not line.startswith("| ---")
    ]
    assert len(rows) == 2, (
        f"expected a header row and one data row; the script emitted:\n{emitted}"
    )
    columns = [cell.strip() for cell in rows[0].strip("|").split("|")]
    assert columns == list(_COLUMNS), (
        f"expected columns {_COLUMNS!r}, found {columns!r} in:\n{emitted}"
    )
    values = [cell.strip() for cell in rows[1].strip("|").split("|")]
    assert len(values) == len(_COLUMNS), (
        f"expected {len(_COLUMNS)} columns, found {values} in:\n{emitted}"
    )
    metric_line = next(
        (line for line in stdout.splitlines() if line.startswith(_METRIC_PREFIX)),
        None,
    )
    assert metric_line is not None, (
        f"expected {_METRIC_PREFIX!r} in the workflow output; found:\n{stdout}"
    )
    labels = metric_line.removeprefix(_METRIC_PREFIX).split()
    assert all("=" in label for label in labels), (
        "expected every benchmark-gate metric label to contain '='; the workflow "
        f"emitted:\n{stdout}"
    )
    metric = dict(label.split("=", maxsplit=1) for label in labels)
    return Summary(
        fields=dict(zip(_FIELD_NAMES, values, strict=True)),
        table="\n".join(rows),
        metric=metric,
    )


def run_summary_script(
    *,
    event: str,
    detector: Detector,
    tmp_path: pth.Path,
    workflow_data: Workflow,
) -> Summary:
    """Execute the real summary script and parse its durable outputs.

    Parameters
    ----------
    event : str
        Event class supplied through the workflow environment.
    detector : Detector
        Detector status and path verdict exposed to the workflow script.
    tmp_path : pathlib.Path
        Pytest temporary directory in which to capture the step summary.
    workflow_data : tests.helpers.workflow.Workflow
        Parsed workflow fixture, supplied at test execution rather than import.

    Returns
    -------
    Summary
        Parsed table and bounded metric emitted by the script.

    """
    summary_path = tmp_path / "step-summary.md"
    summary_path.touch()
    completed = _execute_summary_script(
        event=event,
        detector=detector,
        summary_path=summary_path,
        workflow_data=workflow_data,
    )
    return _parse_summary(
        emitted=summary_path.read_text(encoding="utf-8"), stdout=completed.stdout
    )
