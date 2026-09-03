"""Contract tests for Cuprum's shared Namespace Linux runner slice.

The initial migration intentionally covers ordinary repository-owned Linux
jobs only. Native-platform wheel builds and the Ubuntu 24.04 benchmark ratchet
remain on their existing runners until an equivalent profile is measured.
"""

from __future__ import annotations

import types
import typing as typ
from pathlib import Path

import pytest
import yaml

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from tests.helpers.workflow_types import Job, Step

ROOT = Path(__file__).resolve().parents[1]
BUILD_PROFILE = "namespace-profile-rust-linux-ci"
LIGHT_PROFILE = "namespace-profile-rust-linux-light"
NSCLOUD_CACHE = (
    "namespacelabs/nscloud-cache-action@c5f8dab7560444c4bf8dbc64f1b203431873c547"
)
NEXTEST_INSTALLER = "taiki-e/install-action@18b1216eba7f8039b0f8d131d5473787f0edce68"
SETUP_RUST = (
    "leynos/shared-actions/.github/actions/setup-rust@"
    "5daae0a332441d170d88ca648c9e71f0bbe96cb3"
)
GENERATE_COVERAGE = (
    "leynos/shared-actions/.github/actions/generate-coverage@"
    "5daae0a332441d170d88ca648c9e71f0bbe96cb3"
)
CACHE_STEP_ID = "namespace-cache"
CACHE_REPORT_STEP = "Report Namespace cache status"

# Module-level manifests are read by several tests in one process, so they are
# published as read-only views rather than shared mutable state.
MIGRATED_JOBS: typ.Final = types.MappingProxyType({
    LIGHT_PROFILE: types.MappingProxyType({
        "build-wheels.yml": ("build-pure-wheel", "verify-wheel-install"),
        "ci.yml": ("typecheck-test", "coverage"),
        "coverage-main.yml": ("coverage-upload",),
        "get-codescene-sha.yml": ("refresh-sha",),
        "release.yml": ("publish",),
    }),
    BUILD_PROFILE: types.MappingProxyType({"ci.yml": ("extension-tests",)}),
})
CACHED_JOBS: typ.Final = types.MappingProxyType({
    "build-wheels.yml": ("build-pure-wheel",),
    "ci.yml": ("typecheck-test", "extension-tests", "coverage"),
    "coverage-main.yml": ("coverage-upload",),
})
# Jobs whose dependency installation runs through Make. `Makefile` pins
# `UV_CACHE_DIR=.uv-cache` and `UV_TOOL_DIR=.uv-tools`, so uv's standard
# directories stay empty and only the worktree-local pair is worth mounting.
MAKE_DRIVEN_JOBS: typ.Final = types.MappingProxyType({
    "ci.yml": ("typecheck-test", "extension-tests", "coverage"),
    "coverage-main.yml": ("coverage-upload",),
})
# The cache action forwards each path to `nsc` unresolved, so these are
# spelled absolutely rather than relying on the job's working directory.
MAKE_UV_PATHS: typ.Final = (
    "${{ github.workspace }}/.uv-cache",
    "${{ github.workspace }}/.uv-tools",
)


def _mapping(value: object, message: str) -> dict[str, object]:
    """Narrow a parsed YAML value to a string-keyed mapping."""
    assert isinstance(value, dict), message
    assert all(isinstance(key, str) for key in value), message
    return typ.cast("dict[str, object]", value)


def _jobs(workflow_name: str) -> dict[str, object]:
    """Load the jobs mapping from one repository workflow."""
    path = ROOT / ".github" / "workflows" / workflow_name
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    # YAML 1.1 reads the `on:` trigger key as the boolean ``True``, so the
    # document itself is not string-keyed. Only the jobs mapping is narrowed.
    assert isinstance(document, dict), f"{workflow_name} must parse to a mapping"
    return _mapping(document.get("jobs"), f"{workflow_name} must declare jobs")


def _job(workflow_name: str, job_name: str) -> Job:
    """Return one named job from a repository workflow."""
    job = _mapping(
        _jobs(workflow_name).get(job_name),
        f"{workflow_name} must define {job_name}",
    )
    return typ.cast("Job", job)


def _steps(workflow_name: str, job_name: str) -> list[Step]:
    """Return the validated steps for one workflow job."""
    steps = _job(workflow_name, job_name).get("steps")
    assert isinstance(steps, list), f"{workflow_name}:{job_name} must declare steps"
    return [
        typ.cast(
            "Step",
            _mapping(
                step, f"{workflow_name}:{job_name} step {index} must be a mapping"
            ),
        )
        for index, step in enumerate(steps)
    ]


def _step_inputs(step: Step, message: str) -> dict[str, object]:
    """Return the ``with`` mapping declared by one workflow step."""
    return _mapping(step.get("with"), message)


def _cache_step(workflow_name: str, job_name: str) -> Step:
    """Return the single Namespace cache owner declared by one job."""
    cache_steps = [
        step
        for step in _steps(workflow_name, job_name)
        if step.get("uses") == NSCLOUD_CACHE
    ]
    assert len(cache_steps) == 1, (
        f"{workflow_name}:{job_name} must have one Namespace cache owner"
    )
    return cache_steps[0]


def _cached_paths(workflow_name: str, job_name: str) -> list[str]:
    """Return the paths mounted by one job's Namespace cache owner."""
    cache_inputs = _step_inputs(
        _cache_step(workflow_name, job_name),
        f"{workflow_name}:{job_name} cache step must declare paths",
    )
    paths = cache_inputs.get("path", "")
    assert isinstance(paths, str), (
        f"{workflow_name}:{job_name} cache paths must be a newline-delimited string"
    )
    return [line.strip() for line in paths.splitlines() if line.strip()]


def _expand(manifest: cabc.Mapping[str, tuple[str, ...]]) -> list[tuple[str, str]]:
    """Flatten a workflow-to-job-names manifest into per-job cases."""
    return [
        (workflow_name, job_name)
        for workflow_name, job_names in manifest.items()
        for job_name in job_names
    ]


CACHED_JOB_CASES = _expand(CACHED_JOBS)
MAKE_DRIVEN_JOB_CASES = _expand(MAKE_DRIVEN_JOBS)
PROFILE_CASES = [
    (profile, workflow_name, job_name)
    for profile, manifest in MIGRATED_JOBS.items()
    for workflow_name, job_name in _expand(manifest)
]


@pytest.mark.parametrize(("profile", "workflow_name", "job_name"), PROFILE_CASES)
def test_migrated_jobs_use_their_assigned_shared_profile(
    profile: str, workflow_name: str, job_name: str
) -> None:
    """Keep each migrated job on the shared profile its workload was sized for."""
    actual_runner = _job(workflow_name, job_name).get("runs-on")
    assert actual_runner == profile, (
        f"{workflow_name}:{job_name} must run on {profile}, got {actual_runner!r}"
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


@pytest.mark.parametrize(("workflow_name", "job_name"), CACHED_JOB_CASES)
def test_expensive_namespace_jobs_have_one_cache_owner(
    workflow_name: str, job_name: str
) -> None:
    """Keep external cache setup ahead of dependency and build work."""
    steps = _steps(workflow_name, job_name)
    cache_step = _cache_step(workflow_name, job_name)
    cache_inputs = _step_inputs(
        cache_step, f"{workflow_name}:{job_name} cache step must declare paths"
    )
    assert "cache" not in cache_inputs, (
        f"{workflow_name}:{job_name} must not use command-dependent cache modes"
    )
    assert "~/.cache/uv" in _cached_paths(workflow_name, job_name), (
        f"{workflow_name}:{job_name} must retain uv downloads"
    )
    work_indices = [
        index
        for index, step in enumerate(steps)
        if str(step.get("name", "")).lower().startswith(("install", "build", "run"))
    ]
    if work_indices:
        assert steps.index(cache_step) < min(work_indices), (
            f"{workflow_name}:{job_name} must mount its cache before work"
        )


@pytest.mark.parametrize(("workflow_name", "job_name"), CACHED_JOB_CASES)
def test_namespace_jobs_report_the_cache_hit_in_the_summary(
    workflow_name: str, job_name: str
) -> None:
    """Make a cold volume readable from the run summary rather than inferred."""
    assert _cache_step(workflow_name, job_name).get("id") == CACHE_STEP_ID, (
        f"{workflow_name}:{job_name} cache step must be identified as "
        f"{CACHE_STEP_ID} so the summary can reference its output"
    )
    report_steps = [
        step
        for step in _steps(workflow_name, job_name)
        if step.get("name") == CACHE_REPORT_STEP
    ]
    assert len(report_steps) == 1, (
        f"{workflow_name}:{job_name} must report cache-hit in the summary"
    )
    script = report_steps[0].get("run")
    assert isinstance(script, str), (
        f"{workflow_name}:{job_name} cache report must run a script"
    )
    assert f"steps.{CACHE_STEP_ID}.outputs.cache-hit" in script, (
        f"{workflow_name}:{job_name} cache report must read the cache-hit output"
    )
    assert "GITHUB_STEP_SUMMARY" in script, (
        f"{workflow_name}:{job_name} cache report must write the step summary"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), MAKE_DRIVEN_JOB_CASES)
def test_make_driven_jobs_cache_the_worktree_uv_directories(
    workflow_name: str, job_name: str
) -> None:
    """Cache the uv directories Make pins rather than uv's default locations."""
    cached_paths = _cached_paths(workflow_name, job_name)
    for expected in MAKE_UV_PATHS:
        assert expected in cached_paths, (
            f"{workflow_name}:{job_name} installs through Make, so it must mount "
            f"{expected}; got {cached_paths}"
        )


@pytest.mark.parametrize("job_name", ["typecheck-test", "extension-tests", "coverage"])
def test_sccache_jobs_mount_the_installed_tool_parent(job_name: str) -> None:
    """Prevent Namespace from turning the cached sccache binary into a directory."""
    cached_paths = _cached_paths("ci.yml", job_name)
    assert "~/.local/bin" in cached_paths, (
        f"ci.yml:{job_name} must mount the installed-tool parent"
    )
    assert "~/.local/bin/sccache" not in cached_paths, (
        f"ci.yml:{job_name} must not mount a binary as a directory"
    )


def test_namespace_rust_jobs_disable_nested_cache_owners() -> None:
    """Ensure setup-rust delegates Cargo and uv ownership to Namespace."""
    for job_name in ("typecheck-test", "extension-tests", "coverage"):
        steps = _steps("ci.yml", job_name)
        setup_steps = [step for step in steps if step.get("uses") == SETUP_RUST]
        assert len(setup_steps) == 1, f"ci.yml:{job_name} must use shared setup-rust"
        with_values = _step_inputs(
            setup_steps[0], f"ci.yml:{job_name} setup-rust must declare inputs"
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
    with_values = _step_inputs(
        coverage_step, f"{workflow_name}:{job_name} coverage must declare inputs"
    )
    assert with_values.get("cache-provider") == "external", (
        f"{workflow_name}:{job_name} must delegate cache ownership externally"
    )


def test_coverage_checkouts_do_not_persist_credentials() -> None:
    """Keep the scoped token out of .git/config where project code could read it."""
    for workflow_name, job_name in (
        ("ci.yml", "coverage"),
        ("coverage-main.yml", "coverage-upload"),
    ):
        checkout = next(
            step
            for step in _steps(workflow_name, job_name)
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        with_values = _step_inputs(
            checkout, f"{workflow_name}:{job_name} checkout must declare inputs"
        )
        assert with_values.get("persist-credentials") is False, (
            f"{workflow_name}:{job_name} must set persist-credentials: false"
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
        if "cargo binstall" in content:
            assert "--disable-strategies compile" in content, (
                f"{workflow_path.name} must stop cargo-binstall falling back to "
                "a source build"
            )


def test_nextest_installer_fails_closed() -> None:
    """Fail the job rather than compiling nextest when no binary is published."""
    installer = next(
        step
        for step in _steps("ci.yml", "typecheck-test")
        if step.get("uses") == NEXTEST_INSTALLER
    )
    with_values = _step_inputs(
        installer, "ci.yml:typecheck-test nextest installer must declare inputs"
    )
    assert with_values.get("fallback") == "none", (
        "ci.yml:typecheck-test must disable the install-action source fallback"
    )


def test_lint_tool_install_is_version_pinned() -> None:
    """Keep the npm Markdown linter on a reproducible release."""
    install_step = next(
        step
        for step in _steps("ci.yml", "lint-test")
        if step.get("name") == "Install CLI tools"
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
