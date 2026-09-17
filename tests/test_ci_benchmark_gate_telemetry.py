"""Pin the secret-free persistence and bounded-label workflow contract."""

from __future__ import annotations

import typing as typ

from tests.helpers.benchmark_gate_telemetry import (
    LOG_STEP,
    UPLOAD_STEP,
    VALUE_INPUTS,
    log_env,
    log_script,
)
from tests.helpers.workflow import CHANGES_JOB, mapping, script_of, step_named, steps

if typ.TYPE_CHECKING:
    from tests.helpers.workflow import Workflow


def test_decision_logs_need_no_external_service(workflow_data: Workflow) -> None:
    """Persist decisions using existing GitHub storage without a service token."""
    declared = step_named(workflow_data, CHANGES_JOB, LOG_STEP)
    assert declared.get("continue-on-error") is True, "logging must fail open"
    assert declared.get("if") == "${{ !cancelled() }}", (
        "detector failure must not suppress the decision record"
    )
    env = log_env(workflow_data)
    assert set(env) == set(VALUE_INPUTS), "only gate outputs may enter log labels"
    for name in VALUE_INPUTS:
        assert env[name] == "${{ steps.gate.outputs." + name.lower() + " }}", (
            f"{name} must reuse the canonical gate output"
        )
    source = log_script(workflow_data)
    assert "curl" not in source, "logging must not require external transport"
    assert "secrets." not in source, "logging must not require credentials"
    names = [step.get("name") for step in steps(workflow_data, CHANGES_JOB)]
    assert names.index("Record the benchmark gate decision") < names.index(LOG_STEP), (
        "persist the verdict only after the canonical decision step"
    )


def test_archive_retention_and_fail_open_contract(workflow_data: Workflow) -> None:
    """Archive one log per attempt without changing benchmark admission."""
    archive = step_named(workflow_data, CHANGES_JOB, UPLOAD_STEP)
    assert archive.get("uses") == (
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    ), "reuse the repository's pinned artefact uploader"
    assert archive.get("continue-on-error") is True, "upload failure must fail open"
    condition = str(archive.get("if", ""))
    for required in (
        "!cancelled()",
        "steps.gate-log.outputs.written == 'true'",
        "env.ACT != 'true'",
    ):
        assert required in condition, f"archive guard must include {required}"
    inputs = mapping(archive.get("with"), "archive must declare inputs")
    assert set(inputs) == {"name", "path", "retention-days", "if-no-files-found"}, (
        "archive inputs must contain only the documented storage settings"
    )
    assert inputs["name"] == "benchmark-gate-decision-${{ github.run_attempt }}", (
        "rerun attempts must receive distinct artefact names"
    )
    assert inputs["path"] == "${{ runner.temp }}/benchmark-gate/decisions.jsonl", (
        "upload only the bounded decision record"
    )
    assert inputs["retention-days"] == 90, "request the documented 90-day retention"
    assert inputs["if-no-files-found"] == "error", "missing records must be visible"
    warning = step_named(
        workflow_data, CHANGES_JOB, "Warn when the benchmark gate log is unavailable"
    )
    assert warning.get("if") == (
        "${{ !cancelled() && steps.gate-log-upload.outcome == 'failure' }}"
    ), "failed uploads must produce a visible warning even after detector failure"
    assert "::warning" in (script_of(warning) or ""), "upload failure needs a warning"


def test_measurement_reports_have_the_same_retention(workflow_data: Workflow) -> None:
    """Keep numerical benchmark reports alongside the decision log window."""
    report = step_named(
        workflow_data, "benchmark-ratchet", "Upload benchmark ratchet artefacts"
    )
    inputs = mapping(report.get("with"), "benchmark report upload must declare inputs")
    assert inputs.get("retention-days") == 90, (
        "retain benchmark measurements for 90 days"
    )
