"""Exercise the workflow log writer and inspect its persisted JSON records."""

from __future__ import annotations

import dataclasses as dc
import datetime as dt
import json
import typing as typ

import pytest

from tests.helpers.benchmark_gate_telemetry import (
    LABEL_NAMES,
    Verdict,
    run_log_script,
)
from tests.helpers.workflow import mapping

if typ.TYPE_CHECKING:
    import pathlib as pth

    from tests.helpers.workflow import Workflow

RECORDED_RUN = Verdict("pull_request", "success", "run")
VOCABULARIES = {
    "event_class": {"pull_request", "other"},
    "detector_status": {"success", "failure", "unknown"},
    "decision": {"run", "skip", "skip-detector-failed"},
}


@pytest.mark.parametrize(
    "verdict",
    [
        Verdict(event_class, detector_status, decision)
        for event_class in sorted(VOCABULARIES["event_class"])
        for detector_status in sorted(VOCABULARIES["detector_status"])
        for decision in sorted(VOCABULARIES["decision"])
    ],
)
def test_closed_label_combinations_are_stored_verbatim(
    tmp_path: pth.Path,
    workflow_data: Workflow,
    *,
    verdict: Verdict,
) -> None:
    """Every permitted label combination remains unchanged in the real file."""
    run = run_log_script(
        verdict=verdict, workflow_data=workflow_data, tmp_path=tmp_path
    )
    assert run.exit_code == 0, f"log writer failed: {run.stderr}"
    assert len(run.body.splitlines()) == 1, "one execution must persist one JSON line"
    decoded: object = json.loads(run.body)
    record = mapping(decoded, "decision record must be a mapping")
    assert set(record) == {
        "schema_version",
        "metric",
        "value",
        "labels",
        "run_id",
        "run_attempt",
        "recorded_at",
    }, "the record must expose only the documented schema"
    assert record["schema_version"] == 1, "record schema must be explicitly versioned"
    assert record["metric"] == "benchmark_gate_decisions_total", (
        "metric name must stay stable"
    )
    assert record["value"] == 1, "each record represents exactly one gate observation"
    labels = mapping(record["labels"], "metric labels must be a mapping")
    assert set(labels) == set(LABEL_NAMES), "metadata must never become metric labels"
    assert labels == verdict.as_labels(), "stored labels must reuse the gate outputs"
    assert record["run_id"] == "123456789", "run ID must remain separate metadata"
    assert record["run_attempt"] == "2", "attempt identity supports deduplication"
    timestamp = record["recorded_at"]
    assert isinstance(timestamp, str), "record time must be an ISO timestamp"
    assert timestamp.endswith("Z"), "record time must explicitly use UTC"
    assert dt.datetime.fromisoformat(timestamp).tzinfo is not None, (
        "record time must be aware"
    )
    assert run.body.endswith("\n"), "records must concatenate as valid JSON Lines"
    assert run.outputs == "written=true\nrecord=" + run.body, (
        "outputs must match the persisted file"
    )


@pytest.mark.parametrize("label", tuple(VOCABULARIES))
@pytest.mark.parametrize("value", ["", "docs/private.txt", '"}\nprivate-command'])
def test_invalid_labels_are_refused_without_leaking_them(
    tmp_path: pth.Path, workflow_data: Workflow, *, label: str, value: str
) -> None:
    """Unbounded inputs must never enter records, outputs, or diagnostics."""
    run = run_log_script(
        verdict=dc.replace(RECORDED_RUN, **{label: value}),
        workflow_data=workflow_data,
        tmp_path=tmp_path,
    )
    assert run.exit_code == 0, "invalid logging input must not fail CI"
    assert not run.body, "invalid input must not produce an artefact"
    assert not run.outputs, "invalid input must not advertise a record"
    assert "::warning" in run.stdout, "omitted logs need a visible warning"
    if value:
        assert value not in run.stdout + run.stderr, (
            "diagnostics must not echo invalid data"
        )


def test_storage_failure_does_not_fail_the_gate(
    tmp_path: pth.Path, workflow_data: Workflow
) -> None:
    """A blocked output directory must warn without affecting admission."""
    (tmp_path / "benchmark-gate").write_text("not a directory", encoding="utf-8")
    run = run_log_script(
        verdict=RECORDED_RUN, workflow_data=workflow_data, tmp_path=tmp_path
    )
    assert run.exit_code == 0, "local storage failure must remain fail-open"
    assert not run.body, "failed writes must not leave a record"
    assert not run.outputs, "failed writes must not advertise a record"
    assert "::warning" in run.stdout, "storage failures need a visible warning"
