"""Contract tests for the mutation-testing caller workflow.

The executable logic lives in the ``leynos/shared-actions`` reusable
workflow, which carries its own unit and integration tests; cuprum's
caller is declarative configuration. These tests parse the caller with
PyYAML and assert the contract it must uphold, so drift (repointing the
pin at a branch, widening permissions, or losing the mutmut caller
configuration) fails CI on the pull request rather than surfacing in a
scheduled or manual run.

The caller must reference the correct reusable workflow at a commit
SHA; Dependabot owns the SHA value itself, so these tests match the
pin shape (a 40-character lowercase hex commit SHA) rather than a
specific documented SHA. That keeps routine Dependabot bump PRs from
failing this contract.

Only the Python package is enrolled: the pinned mutation-cargo.yml
reusable workflow always fans a repository-root cargo-mutants target
out for ``workflow_dispatch`` full runs, and cuprum has no
repository-root Cargo.toml, so a Rust job cannot ride the shared
workflow at this pin.

The caller also declares ``baseline-diagnostics``, which reruns the
baseline only when the shared job failed. The shared workflow writes its
results and statistics artefacts only after a successful run, so a
baseline failure would otherwise leave nothing to diagnose but an
expiring step log; ``leynos/shared-actions#591`` tracks the shared fix.
These tests pin that job's failure-only condition, its least-privilege
permissions, the mutmut release it reruns, and the unconditional upload
of its captured output.
"""

from __future__ import annotations

import re
import typing as typ
from pathlib import Path

from tests.helpers.strict_yaml import load

WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "mutation-testing.yml"
)

#: Matches the caller pinning the correct reusable workflow path to a
#: full 40-character lowercase hex commit SHA, without asserting which
#: SHA: Dependabot owns that value.
USES_RE = re.compile(
    r"^leynos/shared-actions/\.github/workflows/mutation-mutmut\.yml@[0-9a-f]{40}$"
)

#: The exact caller configuration: flat layout, so change detection
#: watches the cuprum/ package and no src/ prefix is stripped before
#: module-glob translation. mutmut is pinned to 3.7.0 because 3.6.0's
#: async-generator trampoline defers cleanup until loop shutdown, which
#: fails the unchanged-source baseline.
EXPECTED_WITH = {
    "paths": "cuprum/",
    "module-prefix-strip": "",
    "mutmut-version": "3.7.0",
}

#: Name of the failure-only job that keeps a failing baseline's output.
DIAGNOSTICS_JOB = "baseline-diagnostics"

#: Artefact name and captured file for the baseline diagnostics.
DIAGNOSTICS_ARTEFACT = "mutation-baseline-diagnostics"
DIAGNOSTICS_FILE = "mutation-baseline-diagnostics.txt"

#: Any ``uses:`` other than the reusable workflow call must pin a commit SHA.
PINNED_ACTION_RE = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def _load() -> dict[typ.Any, typ.Any]:
    """Parse the workflow file."""
    workflow = load(WORKFLOW_PATH.read_text(encoding="utf-8"), WORKFLOW_PATH.name)
    assert isinstance(workflow, dict), "the workflow must parse to a mapping"
    return workflow


def _triggers(workflow: dict[typ.Any, typ.Any]) -> dict[typ.Any, typ.Any]:
    """Return the ``on:`` mapping (PyYAML parses the bare key as True)."""
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict), "the workflow must declare an on: mapping"
    return triggers


def _jobs(workflow: dict[typ.Any, typ.Any]) -> dict[typ.Any, typ.Any]:
    """Return the declared jobs mapping."""
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "the workflow must declare a jobs mapping"
    return jobs


def _mutation_job(workflow: dict[typ.Any, typ.Any]) -> dict[typ.Any, typ.Any]:
    """Return the job calling the shared reusable workflow."""
    return _jobs(workflow)["mutation-python"]


def _diagnostics_job(workflow: dict[typ.Any, typ.Any]) -> dict[typ.Any, typ.Any]:
    """Return the failure-only baseline diagnostics job."""
    return _jobs(workflow)[DIAGNOSTICS_JOB]


def _steps(job: dict[typ.Any, typ.Any]) -> list[dict[typ.Any, typ.Any]]:
    """Return a job's steps."""
    steps = job.get("steps")
    assert isinstance(steps, list), "the job must declare a steps list"
    return steps


def _step_using(job: dict[typ.Any, typ.Any], prefix: str) -> dict[typ.Any, typ.Any]:
    """Return the single step whose ``uses`` starts with ``prefix``."""
    matches = [
        step for step in _steps(job) if str(step.get("uses", "")).startswith(prefix)
    ]
    assert len(matches) == 1, (
        f"expected exactly one step using {prefix}, found {len(matches)}"
    )
    return matches[0]


def test_uses_reference_is_pinned_to_a_commit_sha() -> None:
    """The job must call mutation-mutmut.yml pinned to a full commit SHA."""
    uses = _mutation_job(_load()).get("uses")
    assert uses is not None, "jobs.mutation-python.uses is missing"
    assert USES_RE.match(uses), (
        f"jobs.mutation-python.uses must reference "
        "leynos/shared-actions/.github/workflows/mutation-mutmut.yml pinned "
        f"to a full 40-character lowercase hex commit SHA (not a branch or "
        f"tag), got {uses!r}"
    )


def test_job_permissions_are_exactly_least_privilege() -> None:
    """The job grants contents: read and id-token: write, nothing broader."""
    permissions = _mutation_job(_load()).get("permissions")
    assert permissions == {"contents": "read", "id-token": "write"}, (
        "jobs.mutation-python.permissions must be exactly "
        f"{{'contents': 'read', 'id-token': 'write'}}, got {permissions!r}"
    )


def test_workflow_default_permissions_are_empty() -> None:
    """The workflow-level default token scope is empty."""
    workflow = _load()
    assert workflow.get("permissions") == {}, (
        f"top-level permissions must be an empty mapping, got "
        f"{workflow.get('permissions')!r}"
    )


def test_concurrency_serializes_per_ref_without_cancelling() -> None:
    """Runs queue per ref instead of cancelling one another."""
    concurrency = _load().get("concurrency")
    assert isinstance(concurrency, dict), "the workflow must declare concurrency"
    assert concurrency.get("group") == "mutation-testing-${{ github.ref }}", (
        f"concurrency.group must key on the triggering ref, got "
        f"{concurrency.get('group')!r}"
    )
    assert concurrency.get("cancel-in-progress") is False, (
        f"concurrency.cancel-in-progress must be false, got "
        f"{concurrency.get('cancel-in-progress')!r}"
    )


def test_triggers_keep_schedule_and_plain_dispatch() -> None:
    """The daily schedule stays; dispatch declares no inputs."""
    triggers = _triggers(_load())
    schedule = triggers.get("schedule")
    assert schedule == [{"cron": "20 8 * * *"}], (
        f"on.schedule must be the daily 08:20 UTC cron, got {schedule!r}"
    )
    assert "workflow_dispatch" in triggers, "on.workflow_dispatch is missing"
    dispatch = triggers.get("workflow_dispatch") or {}
    inputs = dispatch.get("inputs") or {}
    assert not inputs, (
        f"on.workflow_dispatch must not declare inputs; the Actions "
        f"run-workflow control selects the ref, got {inputs!r}"
    )


def test_with_block_is_exactly_the_documented_configuration() -> None:
    """The caller passes exactly the flat-layout mutmut configuration."""
    with_block = _mutation_job(_load()).get("with")
    assert with_block == EXPECTED_WITH, (
        f"jobs.mutation-python.with must be exactly {EXPECTED_WITH!r} "
        "(flat cuprum/ layout: watch the package, strip no prefix, and pin "
        f"the mutmut release with the async-generator fix), got {with_block!r}"
    )


def test_jobs_are_the_mutation_run_and_its_diagnostics() -> None:
    """The workflow carries the shared call plus one diagnostics job."""
    jobs = _jobs(_load())
    assert list(jobs) == ["mutation-python", DIAGNOSTICS_JOB], (
        "the workflow must declare the shared mutation call and exactly one "
        f"baseline diagnostics job, in that order; found {list(jobs)}"
    )


def test_diagnostics_job_runs_only_after_a_failed_mutation_run() -> None:
    """The diagnostics job needs the mutation run and selects failures."""
    job = _diagnostics_job(_load())
    assert job.get("needs") == "mutation-python", (
        "the diagnostics job must observe the mutation job's outcome, got "
        f"needs={job.get('needs')!r}"
    )
    condition = str(job.get("if", ""))
    assert "failure()" in condition, (
        "the diagnostics job must run only when the mutation run failed, got "
        f"if={condition!r}"
    )


def test_diagnostics_job_permissions_are_least_privilege() -> None:
    """The diagnostics job only reads the repository."""
    permissions = _diagnostics_job(_load()).get("permissions")
    assert permissions == {"contents": "read"}, (
        "jobs.baseline-diagnostics.permissions must be exactly "
        f"{{'contents': 'read'}}, got {permissions!r}"
    )


def test_diagnostics_job_reproduces_the_pinned_mutmut_run() -> None:
    """The job reruns mutmut at the version the caller pins."""
    steps = _steps(_diagnostics_job(_load()))
    scripts = [str(step.get("run", "")) for step in steps if "run" in step]
    matches = [script for script in scripts if "mutmut run" in script]
    assert len(matches) == 1, (
        "the diagnostics job must have exactly one step that runs mutmut, "
        f"found {len(matches)}"
    )
    script = matches[0]
    pinned = f"uv run --with mutmut=={EXPECTED_WITH['mutmut-version']} mutmut run"
    assert pinned in script, (
        "the diagnostics run must reproduce the caller's pinned mutmut "
        f"release; expected {pinned!r} in the step script"
    )
    assert f"tee {DIAGNOSTICS_FILE}" in script, (
        "the diagnostics run must capture its output to "
        f"{DIAGNOSTICS_FILE!r} so the upload step has a file to publish"
    )


def test_diagnostics_upload_keeps_the_captured_output_on_failure() -> None:
    """The upload runs unconditionally and refuses to publish nothing."""
    job = _diagnostics_job(_load())
    upload = _step_using(job, "actions/upload-artifact@")
    condition = str(upload.get("if", ""))
    assert "always()" in condition, (
        "the upload must run even though the reproduced baseline fails, got "
        f"if={condition!r}"
    )
    with_block = upload.get("with")
    assert isinstance(with_block, dict), "the upload step needs a with: block"
    assert with_block.get("name") == DIAGNOSTICS_ARTEFACT, (
        f"the artefact must be named {DIAGNOSTICS_ARTEFACT!r}, got "
        f"{with_block.get('name')!r}"
    )
    assert DIAGNOSTICS_FILE in str(with_block.get("path", "")), (
        f"the upload must publish {DIAGNOSTICS_FILE!r}, got "
        f"path={with_block.get('path')!r}"
    )
    assert with_block.get("if-no-files-found") == "error", (
        "a run with no captured diagnostics must fail loudly rather than "
        f"upload nothing; got {with_block.get('if-no-files-found')!r}"
    )


def test_every_diagnostics_action_is_pinned_to_a_commit_sha() -> None:
    """The new job's actions carry the same pin shape as the rest."""
    steps = _steps(_diagnostics_job(_load()))
    uses = [str(step["uses"]) for step in steps if "uses" in step]
    assert uses, "the diagnostics job must use at least one action"
    for reference in uses:
        assert PINNED_ACTION_RE.match(reference), (
            "every action in the diagnostics job must be pinned to a full "
            f"40-character lowercase hex commit SHA, got {reference!r}"
        )
