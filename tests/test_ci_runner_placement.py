"""Contract tests for where Cuprum's CI jobs run and how wide they fan out.

Runner placement and worker counts are declarations no functional test can
reach: a job that drifts back to a GitHub-hosted label, or that asks
`pytest-xdist` for more workers than the runner has cores, still produces a
green suite while queueing for hours or thrashing two vCPUs. These tests read
the declarations back against the manifests in `tests/helpers/ci_runners.py`.
"""

from __future__ import annotations

import re

import pytest
import yaml

from tests.helpers.ci_runners import (
    GITHUB_HOSTED_JOBS,
    GITHUB_LABEL,
    ROOT,
    UBICLOUD_JOBS,
    UBICLOUD_LABEL,
    UBICLOUD_VCPUS,
    expand,
    job,
    step_inputs,
    steps,
    workflow_document,
    workflow_env,
    workflow_sources,
)

ACTIONLINT_CONFIG = ROOT / ".github" / "actionlint.yaml"
MAKEFILE = ROOT / "Makefile"
VCPU_CONSTANT = "LINUX_RUNNER_VCPUS"
#: Any spelling of the tool, so `nextest@` and `cargo-nextest@` both match.
NEXTEST_TOOL_NAME = "nextest"
#: Shell fragments that mean "fetch a binary" rather than "run one".
INSTALL_VERBS = ("install", "curl", "wget")
#: The upstream install host. Its name does not contain "nextest", so it needs
#: its own token, and it only ever appears in an install command.
NEXTEST_INSTALL_HOST = "get.nexte.st"
#: Steps invoking a shared action are exempt: the coverage action installs
#: nextest on purpose, and that is the only sanctioned place.
SHARED_ACTION_PREFIX = "leynos/shared-actions/"
#: Make variables that carry the runner's vCPU count into the test command.
#: Only the pytest one remains here: the Rust suite moved to the coverage job,
#: which bounds itself through `CARGO_BUILD_JOBS` and `NEXTEST_TEST_THREADS`.
PARALLELISM_OVERRIDES = ("PYTEST_CARGO_BUILD_JOBS",)

UBICLOUD_CASES = expand(UBICLOUD_JOBS)
GITHUB_HOSTED_CASES = expand(GITHUB_HOSTED_JOBS)


@pytest.mark.parametrize(("workflow_name", "job_name"), UBICLOUD_CASES)
def test_linux_build_and_test_jobs_use_the_ubicloud_default_shape(
    workflow_name: str, job_name: str
) -> None:
    """Keep every repository-owned Linux gate on the reviewed Ubicloud shape."""
    runner = job(workflow_name, job_name).get("runs-on")
    assert runner == UBICLOUD_LABEL, (
        f"{workflow_name}:{job_name} must run on {UBICLOUD_LABEL}; escalating to a "
        f"larger shape needs recorded measurements, got {runner!r}"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), UBICLOUD_CASES)
def test_ubicloud_jobs_declare_a_timeout(workflow_name: str, job_name: str) -> None:
    """Bound a wedged paid runner rather than paying for its default six hours."""
    timeout = job(workflow_name, job_name).get("timeout-minutes")
    assert isinstance(timeout, int), (
        f"{workflow_name}:{job_name} must declare timeout-minutes, got {timeout!r}"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), GITHUB_HOSTED_CASES)
def test_administrative_and_serial_jobs_stay_github_hosted(
    workflow_name: str, job_name: str
) -> None:
    """Keep sleeping, API-bound, and publish-only work off metered build slots."""
    runner = job(workflow_name, job_name).get("runs-on")
    assert runner == GITHUB_LABEL, (
        f"{workflow_name}:{job_name} must stay on {GITHUB_LABEL}, got {runner!r}"
    )


def test_native_wheel_matrix_keeps_its_platform_runners() -> None:
    """Ubicloud has no Windows or macOS capacity, so the matrix stays hosted."""
    matrix_job = job("build-wheels.yml", "build-native-wheels")
    assert matrix_job.get("runs-on") == "${{ matrix.os }}", (
        "build-wheels.yml:build-native-wheels must keep its platform matrix"
    )
    strategy = matrix_job.get("strategy")
    assert isinstance(strategy, dict), "the native wheel job must declare a strategy"
    matrix = strategy.get("matrix")
    assert isinstance(matrix, dict), "the native wheel strategy must declare a matrix"
    include = matrix.get("include")
    assert isinstance(include, list), "the native wheel matrix must list its legs"
    operating_systems = {entry["os"] for entry in include}
    assert {"windows-2022", "macos-latest", "macos-15-intel"} <= operating_systems, (
        "the native matrix must retain its Windows and macOS legs"
    )


def test_every_workflow_job_appears_in_one_placement_manifest() -> None:
    """Fail on a new job rather than letting it choose a runner unreviewed."""
    declared = {
        (workflow_name, job_name)
        for workflow_name, source in workflow_sources()
        for job_name in (yaml.safe_load(source).get("jobs") or {})
    }
    known = (
        set(UBICLOUD_CASES)
        | set(GITHUB_HOSTED_CASES)
        | {
            ("build-wheels.yml", "build-native-wheels"),
            # Callers of a reusable workflow declare no runner of their own.
            ("ci.yml", "build-wheels"),
            ("release.yml", "build-wheels"),
            ("dependabot-automerge.yml", "automerge"),
            ("mutation-testing.yml", "mutation-python"),
        }
    )
    assert declared == known, (
        "every workflow job must be classified in tests/helpers/ci_runners.py; "
        f"unclassified: {sorted(declared - known)}; stale: {sorted(known - declared)}"
    )


def test_actionlint_registers_exactly_the_self_hosted_labels_in_use() -> None:
    """Register intentional labels so a typo fails lint instead of queueing."""
    config = yaml.safe_load(ACTIONLINT_CONFIG.read_text(encoding="utf-8"))
    declared = config["self-hosted-runner"]["labels"]
    used = {
        str(job(workflow_name, job_name).get("runs-on"))
        for workflow_name, job_name in UBICLOUD_CASES
    }
    assert sorted(declared) == sorted(used), (
        "actionlint must list every self-hosted label the workflows use and no "
        f"others; declared {declared}, used {sorted(used)}"
    )
    assert config["config-variables"] == ["CODESCENE_CLI_SHA256"], (
        "list only the configuration variables the workflows read, so a typo "
        f"in a vars.* reference fails lint; got {config['config-variables']}"
    )


def test_no_retired_runner_labels_remain() -> None:
    """Leave no Namespace label or cache action behind after the migration."""
    for workflow_name, source in workflow_sources():
        assert "namespace-profile" not in source, (
            f"{workflow_name} still references a Namespace runner profile"
        )
        assert "nscloud" not in source, (
            f"{workflow_name} still references the Namespace cache action"
        )


def test_the_vcpu_constant_matches_the_assigned_label() -> None:
    """Tie the one parallelism constant to the shape the job is billed for."""
    declared = workflow_env("ci.yml")[VCPU_CONSTANT]
    assert declared == str(UBICLOUD_VCPUS), (
        f"{VCPU_CONSTANT} must equal the vCPU count of {UBICLOUD_LABEL}, "
        f"got {declared!r}"
    )


def test_python_tests_derive_their_worker_counts_from_that_constant() -> None:
    """Size the matrix suite's Cargo work from the constant, not a literal."""
    script = next(
        step["run"]
        for step in steps("ci.yml", "typecheck-test")
        if step.get("name") == "Run tests"
    )
    assert isinstance(script, str), "ci.yml:typecheck-test must run a test script"
    for variable in PARALLELISM_OVERRIDES:
        assert f'{variable}="${{{VCPU_CONSTANT}}}"' in script, (
            f"ci.yml:typecheck-test must pass {variable} from {VCPU_CONSTANT}"
        )


def test_extension_and_benchmark_builds_are_bounded_too() -> None:
    """Bound the two jobs that compile outside `make test` to the same count."""
    for job_name, step_name in (
        ("extension-tests", "Build the native extension"),
        ("benchmark-ratchet", "Run throughput benchmarks and ratchet comparison"),
    ):
        script = next(
            step["run"]
            for step in steps("ci.yml", job_name)
            if step.get("name") == step_name
        )
        assert isinstance(script, str), f"ci.yml:{job_name} must run {step_name!r}"
        assert f'CARGO_BUILD_JOBS="${{{VCPU_CONSTANT}}}"' in script, (
            f"ci.yml:{job_name} must bound Cargo build jobs by {VCPU_CONSTANT}"
        )


def test_python_suites_never_ask_for_unbounded_workers() -> None:
    """Reject `-n auto`: the runner has two cores whatever the host reports."""
    sources = [source for _, source in workflow_sources()]
    sources.append(MAKEFILE.read_text(encoding="utf-8"))
    for source in sources:
        assert "-n auto" not in source, "xdist worker counts must be explicit"


def test_the_python_suite_stays_serial() -> None:
    """Keep pytest serial while its batches contend on one Cargo target."""
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert re.search(r"^PYTEST_WORKERS \?= 0$", makefile, re.MULTILINE), (
        "PYTEST_WORKERS must default to 0; the batches compile and reuse the "
        "same Rust artefacts, so xdist workers would contend on one build lock"
    )
    for workflow_name, job_name in (("ci.yml", "coverage"),):
        coverage_step = next(
            step
            for step in steps(workflow_name, job_name)
            if str(step.get("uses", "")).startswith(
                "leynos/shared-actions/.github/actions/generate-coverage@"
            )
        )
        inputs = step_inputs(coverage_step, f"{workflow_name}:{job_name} inputs")
        workers = inputs.get("pytest-workers")
        assert isinstance(workers, str), (
            f"{workflow_name}:{job_name} coverage must declare pytest-workers"
        )
        assert not workers, (
            f"{workflow_name}:{job_name} coverage must run pytest serially, "
            f"got {workers!r}"
        )


def test_ci_does_not_build_tools_from_source() -> None:
    """Keep CI tool installation on trusted, pinned prebuilt paths."""
    for workflow_name, source in workflow_sources():
        assert "cargo install" not in source, (
            f"{workflow_name} must not source-build a Cargo tool"
        )
        assert "get.nexte.st/latest" not in source, (
            f"{workflow_name} must pin the nextest binary version"
        )
        if "cargo binstall" in source:
            assert "--disable-strategies compile" in source, (
                f"{workflow_name} must stop cargo-binstall falling back to a "
                "source build"
            )


def test_no_workflow_installs_cargo_nextest() -> None:
    """Leave the nextest install to the coverage action that now needs it.

    The matrix jobs stopped running the Rust suite, so an installer here would
    be an unused download whose failure mode nothing in this repository
    exercises.

    Checked structurally rather than by one literal string. A `tool:` input
    naming either spelling, a different installer action, or a shell command
    that fetches nextest would all pass a check for `tool: nextest@`.
    """
    offenders: list[str] = []
    for workflow_name, _ in workflow_sources():
        document = workflow_document(workflow_name)
        jobs_mapping = document.get("jobs")
        if not isinstance(jobs_mapping, dict):
            continue
        for job_name in jobs_mapping:
            offenders.extend(
                _nextest_installers(workflow_name, str(job_name)),
            )
    assert not offenders, (
        f"only the shared coverage action may install cargo-nextest; found {offenders}"
    )


def _nextest_installers(workflow_name: str, job_name: str) -> list[str]:
    """Return descriptions of steps in one job that would install nextest."""
    found: list[str] = []
    try:
        job_steps = steps(workflow_name, job_name)
    except AssertionError:
        # A job that calls a reusable workflow declares no steps of its own.
        return found
    for step in job_steps:
        where = f"{workflow_name}:{job_name}:{step.get('name') or step.get('uses')}"
        uses = str(step.get("uses", ""))
        if uses.startswith(SHARED_ACTION_PREFIX):
            # The coverage action installs nextest deliberately; that is the
            # one place it is meant to happen.
            continue
        inputs = step.get("with")
        if isinstance(inputs, dict) and any(
            NEXTEST_TOOL_NAME in str(value).lower() for value in inputs.values()
        ):
            found.append(f"{where} (installer input)")
        script = str(step.get("run", "")).lower()
        fetches = NEXTEST_TOOL_NAME in script and any(
            verb in script for verb in INSTALL_VERBS
        )
        if fetches or NEXTEST_INSTALL_HOST in script:
            found.append(f"{where} (install command)")
    return found


def test_lint_tool_install_is_version_pinned() -> None:
    """Keep the npm Markdown linter on a reproducible release."""
    script = next(
        step["run"]
        for step in steps("ci.yml", "lint-test")
        if step.get("name") == "Install CLI tools"
    )
    assert isinstance(script, str), "ci.yml:Install CLI tools must run a script"
    assert "markdownlint-cli2@${MARKDOWNLINT_VERSION}" in script, (
        "ci.yml:Install CLI tools must pin markdownlint-cli2"
    )
