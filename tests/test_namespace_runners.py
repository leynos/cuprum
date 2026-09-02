"""Contract tests for Cuprum's shared Namespace Linux runner slice.

The initial migration intentionally covers ordinary repository-owned Linux
jobs only. Native-platform wheel builds and the Ubuntu 24.04 benchmark ratchet
remain on their existing runners until an equivalent profile is measured.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PROFILE = "namespace-profile-default"
MIGRATED_JOBS = {
    "build-wheels.yml": ("build-pure-wheel", "verify-wheel-install"),
    "ci.yml": ("typecheck-test", "extension-tests", "coverage"),
    "coverage-main.yml": ("coverage-upload",),
    "delayed-pr-comment.yml": ("delay_and_comment",),
    "get-codescene-sha.yml": ("refresh-sha",),
    "release.yml": ("publish",),
}


def _jobs(workflow_name: str) -> dict[str, object]:
    """Load the jobs mapping from one repository workflow."""
    path = ROOT / ".github" / "workflows" / workflow_name
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict), f"{workflow_name} must parse to a mapping"
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), f"{workflow_name} must declare jobs"
    return jobs


def _job(workflow_name: str, job_name: str) -> dict[str, object]:
    """Return one named job from a repository workflow."""
    job = _jobs(workflow_name).get(job_name)
    assert isinstance(job, dict), f"{workflow_name} must define {job_name}"
    return typ.cast("dict[str, object]", job)


@pytest.mark.parametrize(("workflow_name", "job_names"), MIGRATED_JOBS.items())
def test_compatible_linux_jobs_use_the_shared_namespace_profile(
    workflow_name: str, job_names: tuple[str, ...]
) -> None:
    """Keep the approved uncached runner assignment from drifting."""
    for job_name in job_names:
        actual_runner = _job(workflow_name, job_name).get("runs-on")
        assert actual_runner == PROFILE, (
            f"{workflow_name}:{job_name} must run on {PROFILE}, got {actual_runner!r}"
        )


def test_specialized_jobs_retain_compatible_runners() -> None:
    """Preserve platform and toolchain contracts outside the pilot slice."""
    lint_runner = _job("ci.yml", "lint-test").get("runs-on")
    assert lint_runner == "ubuntu-latest", (
        "ci.yml:lint-test must retain the Whitaker-compatible GitHub image, "
        f"got {lint_runner!r}"
    )
    native_runner = _job("build-wheels.yml", "build-native-wheels").get("runs-on")
    assert native_runner == "${{ matrix.os }}", (
        "build-wheels.yml:build-native-wheels must keep its platform matrix, "
        f"got {native_runner!r}"
    )
    benchmark_runner = _job("ci.yml", "benchmark-ratchet").get("runs-on")
    assert benchmark_runner == "ubicloud-standard-4-ubuntu-2404", (
        "ci.yml:benchmark-ratchet must retain its Ubuntu 24.04 benchmark "
        f"runner, got {benchmark_runner!r}"
    )
