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
PROFILE = "namespace-profile-cuprum"
NSCLOUD_CACHE = (
    "namespacelabs/nscloud-cache-action@c5f8dab7560444c4bf8dbc64f1b203431873c547"
)
SETUP_RUST = (
    "leynos/shared-actions/.github/actions/setup-rust@"
    "5daae0a332441d170d88ca648c9e71f0bbe96cb3"
)
GENERATE_COVERAGE = (
    "leynos/shared-actions/.github/actions/generate-coverage@"
    "5daae0a332441d170d88ca648c9e71f0bbe96cb3"
)
MIGRATED_JOBS = {
    "build-wheels.yml": ("build-pure-wheel", "verify-wheel-install"),
    "ci.yml": ("typecheck-test", "extension-tests", "coverage"),
    "coverage-main.yml": ("coverage-upload",),
    "get-codescene-sha.yml": ("refresh-sha",),
    "release.yml": ("publish",),
}
CACHED_JOBS = {
    "build-wheels.yml": ("build-pure-wheel",),
    "ci.yml": ("typecheck-test", "extension-tests", "coverage"),
    "coverage-main.yml": ("coverage-upload",),
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


def _steps(workflow_name: str, job_name: str) -> list[dict[str, object]]:
    """Return the steps for one workflow job."""
    steps = _job(workflow_name, job_name).get("steps")
    assert isinstance(steps, list), f"{workflow_name}:{job_name} must declare steps"
    return [typ.cast("dict[str, object]", step) for step in steps]


@pytest.mark.parametrize(("workflow_name", "job_names"), MIGRATED_JOBS.items())
def test_compatible_linux_jobs_use_the_shared_namespace_profile(
    workflow_name: str, job_names: tuple[str, ...]
) -> None:
    """Keep the approved runner assignment from drifting."""
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
    delayed_runner = _job("delayed-pr-comment.yml", "delay_and_comment").get("runs-on")
    assert delayed_runner == "ubuntu-latest", (
        "delayed-pr-comment.yml:delay_and_comment must retain the GitHub-hosted "
        f"runner, got {delayed_runner!r}"
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


@pytest.mark.parametrize(("workflow_name", "job_names"), CACHED_JOBS.items())
def test_expensive_namespace_jobs_have_one_cache_owner(
    workflow_name: str, job_names: tuple[str, ...]
) -> None:
    """Keep external cache setup ahead of dependency and build work."""
    for job_name in job_names:
        steps = _steps(workflow_name, job_name)
        cache_steps = [step for step in steps if step.get("uses") == NSCLOUD_CACHE]
        assert len(cache_steps) == 1, (
            f"{workflow_name}:{job_name} must have one Namespace cache owner"
        )
        cache_index = steps.index(cache_steps[0])
        cache_inputs = cache_steps[0].get("with")
        assert isinstance(cache_inputs, dict), (
            f"{workflow_name}:{job_name} cache step must declare paths"
        )
        assert "cache" not in cache_inputs, (
            f"{workflow_name}:{job_name} must not use command-dependent cache modes"
        )
        cached_paths = str(cache_inputs.get("path", ""))
        assert "~/.cache/uv" in cached_paths, (
            f"{workflow_name}:{job_name} must retain uv downloads"
        )
        work_indices = [
            index
            for index, step in enumerate(steps)
            if str(step.get("name", "")).lower().startswith(("install", "build", "run"))
        ]
        if work_indices:
            assert cache_index < min(work_indices), (
                f"{workflow_name}:{job_name} must mount its cache before work"
            )
        assert any(
            step.get("name") == "Report Namespace cache status" for step in steps
        ), f"{workflow_name}:{job_name} must report cache-hit in the summary"


def test_namespace_rust_jobs_disable_nested_cache_owners() -> None:
    """Ensure setup-rust delegates Cargo and uv ownership to Namespace."""
    for job_name in ("typecheck-test", "extension-tests", "coverage"):
        steps = _steps("ci.yml", job_name)
        setup_steps = [step for step in steps if step.get("uses") == SETUP_RUST]
        assert len(setup_steps) == 1, f"ci.yml:{job_name} must use shared setup-rust"
        with_values = setup_steps[0].get("with")
        assert isinstance(with_values, dict), (
            f"ci.yml:{job_name} setup-rust must declare inputs"
        )
        assert with_values.get("cache-provider") == "external", (
            f"ci.yml:{job_name} must delegate cache ownership externally"
        )
        assert with_values.get("use-sccache") == "false", (
            f"ci.yml:{job_name} must disable the nested sccache owner"
        )


@pytest.mark.parametrize(
    ("workflow_name", "job_name"),
    [("ci.yml", "coverage"), ("coverage-main.yml", "coverage-upload")],
)
def test_coverage_delegates_archive_cache_ownership(
    workflow_name: str, job_name: str
) -> None:
    """Ensure coverage leaves dependency archives to Namespace."""
    coverage_step = next(
        step
        for step in _steps(workflow_name, job_name)
        if step.get("uses") == GENERATE_COVERAGE
    )
    with_values = coverage_step.get("with")
    assert isinstance(with_values, dict), (
        f"{workflow_name}:{job_name} coverage must declare inputs"
    )
    assert with_values.get("cache-provider") == "external", (
        f"{workflow_name}:{job_name} must delegate cache ownership externally"
    )


def test_ci_does_not_build_tools_from_source() -> None:
    """Keep CI tool installation on trusted, pinned prebuilt paths."""
    for workflow_path in (ROOT / ".github" / "workflows").glob("*.yml"):
        content = workflow_path.read_text(encoding="utf-8")
        assert "cargo install" not in content, (
            f"{workflow_path.name} must not source-build a Cargo tool"
        )
        assert "get.nexte.st/latest" not in content, (
            f"{workflow_path.name} must pin the nextest binary version"
        )


def test_lint_tool_install_is_version_pinned() -> None:
    """Keep the npm Markdown linter on a reproducible release."""
    steps = _job("ci.yml", "lint-test").get("steps")
    assert isinstance(steps, list), "ci.yml:lint-test must declare steps"
    install_step = next(
        step
        for step in steps
        if isinstance(step, dict) and step.get("name") == "Install CLI tools"
    )
    script = install_step.get("run")
    assert isinstance(script, str), "ci.yml:Install CLI tools must run a script"
    assert "markdownlint-cli2@${MARKDOWNLINT_VERSION}" in script, (
        "ci.yml:Install CLI tools must pin markdownlint-cli2"
    )


def test_benchmark_build_has_one_github_cache_owner() -> None:
    """Keep the retained benchmark build from repeating dependency downloads."""
    steps = _steps("ci.yml", "benchmark-ratchet")
    cache_steps = [
        step
        for step in steps
        if step.get("uses") == "actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9"
    ]
    assert len(cache_steps) == 1, (
        "ci.yml:benchmark-ratchet must have one GitHub cache owner"
    )
    cache_index = steps.index(cache_steps[0])
    build_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Run throughput benchmarks and ratchet comparison"
    )
    assert cache_index < build_index, (
        "ci.yml:benchmark-ratchet must mount its cache before the build"
    )
