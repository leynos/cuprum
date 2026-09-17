"""Guard the workflow projection that isolates the runtime admission boundary."""

from __future__ import annotations

import pathlib as pth

import pytest
import yaml

from tests.helpers.act_harness import CI_WORKFLOW
from tests.helpers.act_workflow import copy_workflow
from tests.helpers.ci_workflows import workflow_document
from tests.helpers.workflow import job, mapping, parse_workflow, step_named
from tests.test_ci_workflow_harness_job import (
    test_the_harness_job_runs_the_target_that_refuses_a_skip as verify_harness_command,
)


def test_projection_preserves_the_detector_and_admission_contract(
    tmp_path: pth.Path,
) -> None:
    """Only expensive job bodies may be replaced by lightweight probes."""
    worktree = pth.Path(__file__).resolve().parents[1]
    copy_workflow(tmp_path, worktree, CI_WORKFLOW)
    projected = parse_workflow((tmp_path / CI_WORKFLOW).read_text(encoding="utf-8"))
    source = parse_workflow((worktree / CI_WORKFLOW).read_text(encoding="utf-8"))
    jobs = mapping(projected.get("jobs"), "projection must declare jobs")
    assert job(projected, "changes") == job(source, "changes"), (
        "projection must preserve the entire changes job"
    )
    benchmark = job(projected, "benchmark-ratchet")
    for key in ("needs", "if"):
        assert benchmark[key] == job(source, "benchmark-ratchet")[key], (
            "admission dependencies and condition must match production"
        )
    needs = benchmark["needs"]
    assert isinstance(needs, list), "benchmark dependencies must be a list"
    assert all(isinstance(name, str) for name in needs), (
        "benchmark dependency names must be strings"
    )
    assert set(jobs) == {*needs, "benchmark-ratchet"}, (
        "projection must contain exactly the benchmark dependency graph"
    )
    assert all(job(projected, name)["runs-on"] == "ubuntu-latest" for name in jobs), (
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


@pytest.mark.parametrize(
    ("job_name", "field"), [("changes", "steps"), ("benchmark-ratchet", "needs")]
)
def test_projection_rejects_malformed_workflow_shapes(
    tmp_path: pth.Path, job_name: str, field: str
) -> None:
    """Malformed runtime inputs must fail with a deliberate contract diagnostic."""
    worktree = pth.Path(__file__).resolve().parents[1]
    parsed = parse_workflow((worktree / CI_WORKFLOW).read_text(encoding="utf-8"))
    job(parsed, job_name)[field] = None
    source = tmp_path / "source"
    workflow = source / CI_WORKFLOW
    workflow.parent.mkdir(parents=True)
    workflow.write_text(yaml.safe_dump(parsed), encoding="utf-8")
    with pytest.raises(AssertionError, match="must declare"):
        copy_workflow(tmp_path / "target", source, CI_WORKFLOW)


@pytest.mark.parametrize(
    "script",
    [
        "# make test-act\ntrue",
        "echo 'make test-act'",
        "make test-act-extra",
        "cat <<'EOF'\nmake test-act\nEOF",
    ],
)
def test_harness_contract_rejects_unexecuted_target_text(script: str) -> None:
    """Mentioning the target must not satisfy the workflow execution contract.

    Parameters
    ----------
    script : str
        Shell text assigned to the harness step, mentioning the target without
        invoking it.
    """
    worktree = pth.Path(__file__).resolve().parents[1]
    source = worktree / ".github/workflows/benchmark-gate-harness.yml"
    workflow = parse_workflow(source.read_text(encoding="utf-8"))
    step = step_named(
        workflow, "workflow-harness", "Run the workflow integration harness"
    )
    step["run"] = script
    with pytest.raises(AssertionError, match="must run"):
        verify_harness_command(workflow)
