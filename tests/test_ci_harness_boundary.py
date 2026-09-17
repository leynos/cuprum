"""Guard the workflow projection that isolates the runtime admission boundary."""

from __future__ import annotations

import pathlib as pth

import yaml

from tests.helpers.act_harness import CI_WORKFLOW
from tests.helpers.act_workflow import copy_workflow
from tests.helpers.ci_workflows import workflow_document


def test_projection_preserves_the_detector_and_admission_contract(
    tmp_path: pth.Path,
) -> None:
    """Only expensive job bodies may be replaced by lightweight probes."""
    worktree = pth.Path(__file__).resolve().parents[1]
    copy_workflow(tmp_path, worktree, CI_WORKFLOW)
    projected = yaml.safe_load((tmp_path / CI_WORKFLOW).read_text(encoding="utf-8"))
    source = workflow_document("ci.yml")
    original_jobs = source["jobs"]
    assert isinstance(original_jobs, dict), "source jobs must be a mapping"
    jobs = projected["jobs"]
    assert jobs["changes"] == original_jobs["changes"], (
        "projection must preserve the entire changes job"
    )
    for key in ("needs", "if"):
        assert (
            jobs["benchmark-ratchet"][key] == original_jobs["benchmark-ratchet"][key]
        ), "admission dependencies and condition must match production"
    assert set(jobs) == {*jobs["benchmark-ratchet"]["needs"], "benchmark-ratchet"}, (
        "projection must contain exactly the benchmark dependency graph"
    )
    assert all(job["runs-on"] == "ubuntu-latest" for job in jobs.values()), (
        "all probes must use GitHub-hosted runner mappings"
    )


def test_scheduled_harness_cannot_start_paid_ci_jobs() -> None:
    """The weekly compatibility run must own a separate single-job workflow."""
    harness = workflow_document("benchmark-gate-harness.yml")
    ci = workflow_document("ci.yml")
    harness_jobs = harness["jobs"]
    assert isinstance(harness_jobs, dict), "harness jobs must be a mapping"
    assert set(harness_jobs) == {"workflow-harness"}, (
        "scheduled workflow must contain only the harness"
    )
    triggers = harness.get("on", harness.get(True))
    ci_triggers = ci.get("on", ci.get(True))
    assert isinstance(triggers, dict), "harness triggers must be a mapping"
    assert isinstance(ci_triggers, dict), "CI triggers must be a mapping"
    assert "schedule" in triggers, "dedicated harness must retain its weekly schedule"
    assert "schedule" not in ci_triggers, (
        "CI must not schedule paid jobs for the harness"
    )
    assert "workflow_dispatch" in ci_triggers, (
        "CI must retain manual warm-cache dispatch"
    )
