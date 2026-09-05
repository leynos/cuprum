"""Contract tests for how CI builds the extension before testing against it.

`make develop` is the one definition of the extension build, and nothing else
in the suite notices when a CI job stops going through it: remove the build
step from `extension-tests` and the gated modules quietly skip, drop
`--release` from `benchmark-ratchet` and the ratchet compares debug builds
against optimized baselines. Both are declarative configuration, so
these tests parse `ci.yml` and assert the contract it must uphold.

The Makefile half of the same contract — the guard variable the recipe sets
and the module list it hands to pytest — lives in
`test_extension_build_contract.py`. The parsing lives in
`tests.helpers.workflow`, shared with the tests that assert the path gate in
front of the same workflow's benchmark job.
"""

from __future__ import annotations

import re

import pytest

from tests.helpers.ci_workflows import workflow_document, workflow_sources
from tests.helpers.workflow import (
    Workflow,
    first_step_running,
    job,
    run_scripts,
    script_runs_command,
    step_named,
    steps,
)

SHARED_ACTIONS_REFERENCE = re.compile(
    r"^leynos/shared-actions/(?P<target>[^\s@]+)@(?P<reference>[0-9a-f]{40})$"
)
VALID_COMMIT_SHA = "0123456789abcdef0123456789abcdef01234567"
EXPECTED_SHARED_ACTIONS_TARGETS = {
    "ci.yml": {
        ".github/actions/generate-coverage",
        ".github/actions/install-mdtablefix",
        ".github/actions/install-nixie",
        ".github/actions/install-whitaker",
        ".github/actions/setup-rust",
        ".github/actions/upload-codescene-coverage",
    },
    "coverage-main.yml": {
        ".github/actions/generate-coverage",
        ".github/actions/setup-rust",
        ".github/actions/upload-codescene-coverage",
    },
    "dependabot-automerge.yml": {
        ".github/workflows/dependabot-automerge.yml",
    },
    "mutation-testing.yml": {
        ".github/workflows/mutation-mutmut.yml",
    },
}


def _shared_action_uses(value: object) -> list[str]:
    """Collect shared-actions ``uses`` values from parsed workflow data."""
    if isinstance(value, dict):
        uses = value.get("uses")
        direct_uses = (
            [uses]
            if isinstance(uses, str) and uses.startswith("leynos/shared-actions/")
            else []
        )
        return direct_uses + [
            nested_uses
            for nested_value in value.values()
            for nested_uses in _shared_action_uses(nested_value)
        ]
    if isinstance(value, list):
        return [
            nested_uses
            for nested_value in value
            for nested_uses in _shared_action_uses(nested_value)
        ]
    return []


def _shared_action_target(uses: object, step_name: str) -> str:
    """Return the shared-action target declared by one named workflow step."""
    assert isinstance(uses, str), f"the {step_name!r} step must declare uses"
    reference = SHARED_ACTIONS_REFERENCE.fullmatch(uses)
    assert reference is not None, (
        f"the {step_name!r} step must use a shared action with a revision; "
        f"found {uses!r}"
    )
    return reference["target"]


@pytest.mark.parametrize(
    ("uses", "expected_revision"),
    [
        (
            f"leynos/shared-actions/.github/actions/setup-rust@{VALID_COMMIT_SHA}",
            VALID_COMMIT_SHA,
        ),
        ("leynos/shared-actions/.github/actions/setup-rust@main", None),
        ("leynos/shared-actions/.github/actions/setup-rust@v1.2.3", None),
        ("leynos/shared-actions/.github/actions/setup-rust@", None),
        ("leynos/shared-actions/.github/actions/setup-rust@main # comment", None),
        (
            f"leynos/shared-actions/.github/actions/setup-rust@{VALID_COMMIT_SHA.upper()}",
            None,
        ),
        (
            f"leynos/shared-actions/.github/actions/setup-rust@{VALID_COMMIT_SHA[:8]}",
            None,
        ),
        (
            f"leynos/shared-actions/.github/actions/setup-rust@{VALID_COMMIT_SHA}a",
            None,
        ),
        (
            f"leynos/shared-actions/.github/actions/setup-rust@g{VALID_COMMIT_SHA[1:]}",
            None,
        ),
    ],
)
def test_shared_actions_reference_captures_immutable_commit_sha(
    uses: str, expected_revision: str | None
) -> None:
    """Shared-action callers pin immutable commit SHAs for Dependabot to update."""
    reference = SHARED_ACTIONS_REFERENCE.fullmatch(uses)
    revision = reference["reference"] if reference is not None else None
    assert revision == expected_revision, (
        f"reference {uses!r}: expected revision {expected_revision!r}, got {revision!r}"
    )


def test_the_ci_job_builds_the_extension_before_running_the_gated_tests(
    workflow_data: Workflow,
) -> None:
    """`extension-tests` must run `make develop` first.

    `make build` only syncs dependencies, so this ordering is the whole reason
    the job can pass at all, and nothing else asserts it.

    Parameters
    ----------
    workflow_data : Workflow
        Parsed ``ci.yml`` model used to inspect the extension-tests job.
    """
    build, _ = first_step_running(
        workflow_data, "make develop", job_name="extension-tests"
    )
    tests, _ = first_step_running(
        workflow_data, "make test-extension", job_name="extension-tests"
    )

    assert build < tests, (
        "the extension-tests job must build the extension with `make "
        "develop` before `make test-extension` runs; found the build at step "
        f"{build} and the tests at step {tests}"
    )


@pytest.mark.parametrize(
    ("script", "command", "expected"),
    [
        ("make develop", "make develop", True),
        ("make develop MATURIN_DEVELOP_FLAGS=--release", "make develop", True),
        ("TOOL=rust make develop", "make develop", True),
        ("9TOOL=rust make develop", "make develop", False),
        ("=rust make develop", "make develop", False),
        ("make " + "\\" + "\n" + "develop", "make develop", True),
        ("make build && make develop", "make develop", True),
        ("if make develop", "make develop", True),
        ("then make develop", "make develop", True),
        ("elif make develop", "make develop", True),
        ("else make develop", "make develop", True),
        ("do make develop", "make develop", True),
        ("# make develop", "make develop", False),
        ('echo "make develop"', "make develop", False),
        ("echo if make develop", "make develop", False),
        ("cat <<EOF\nif make develop\nEOF", "make develop", False),
        ("cat <<EOF\nmaturin develop\nEOF", "maturin develop", False),
        (
            "cat <<EOF\nmaturin develop\nEOF\nmaturin develop",
            "maturin develop",
            True,
        ),
        (
            (
                "cat <<FIRST <<SECOND\nignored first\nFIRST\nmaturin develop\n"
                "SECOND\nmake develop"
            ),
            "maturin develop",
            False,
        ),
        ("cat <<-EOF\n\tif make develop\n\tEOF\nmake develop", "make develop", True),
        ("printf '<<' EOF\nmake develop", "make develop", True),
        ("maturin develop", "maturin develop", True),
        ("# maturin develop", "maturin develop", False),
    ],
)
def test_script_runs_command_ignores_comments_and_non_commands(
    script: str,
    command: str,
    *,
    expected: bool,
) -> None:
    """The workflow matcher detects executable commands, not text mentions.

    Parameters
    ----------
    script : str
        Shell script text to inspect for an executable command.
    command : str
        Command invocation that must be recognized in the script.
    expected : bool
        Whether the command is expected to be recognized.

    """
    assert script_runs_command(script, command) is expected, (
        f"expected script {script!r} to match command {command!r} as {expected}"
    )


def test_only_boundary_jobs_build_the_extension(workflow_data: Workflow) -> None:
    """Only jobs isolated from the full suite may install the extension."""
    builders = {
        job_name
        for job_name, script in run_scripts(workflow_data)
        if script_runs_command(script, "make develop")
    }

    assert builders == {"benchmark-ratchet", "extension-tests"}, (
        "only the isolated extension and benchmark jobs may run `make develop`; "
        f"found {builders}"
    )


def test_the_benchmark_job_builds_in_place_through_the_develop_target(
    workflow_data: Workflow,
) -> None:
    """Require an optimized in-place benchmark build via `make develop`.

    Its numbers mean nothing against a debug build, while installing the mixed
    project would resolve lint-only dependencies that the benchmark does not
    use. Both flags matter as much as the shared target does.

    Parameters
    ----------
    workflow_data : Workflow
        Parsed ``ci.yml`` model used to inspect the benchmark-ratchet job.
    """
    _, script = first_step_running(
        workflow_data, "make develop", job_name="benchmark-ratchet"
    )

    assert "MATURIN_DEVELOP_FLAGS='--release --skip-install'" in script, (
        "the benchmark-ratchet job must build with `make develop "
        "MATURIN_DEVELOP_FLAGS='--release --skip-install'`; without these "
        "flags the ratchet either compares debug builds or installs unrelated "
        "dependencies"
    )


def test_no_ci_step_invokes_maturin_develop_directly(workflow_data: Workflow) -> None:
    """`make develop` must be the only definition of the extension build.

    A second copy of the three-step sequence is how the two drift: the copy
    stops matching the target, and whichever job owns it quietly builds
    something nobody maintains.
    """
    offenders = sorted({
        job_name
        for job_name, script in run_scripts(workflow_data)
        if script_runs_command(script, "maturin develop")
    })

    assert not offenders, (
        "these CI jobs invoke `maturin develop` directly instead of going "
        f"through `make develop`: {offenders}"
    )


def test_lint_job_uses_shared_tooling_installers(workflow_data: Workflow) -> None:
    """The lint job uses shared tooling setup in the required order."""
    expected_actions = {
        "Install Rust toolchain for Nixie": ".github/actions/setup-rust",
        "Install Nixie": ".github/actions/install-nixie",
        "Install project Rust toolchain": ".github/actions/setup-rust",
        "Install Whitaker": ".github/actions/install-whitaker",
    }

    for step_name, expected_target in expected_actions.items():
        step = step_named(workflow_data, "lint-test", step_name)
        assert _shared_action_target(step.get("uses"), step_name) == expected_target, (
            f"the {step_name!r} step must use shared action {expected_target}; "
            f"found {step.get('uses')!r}"
        )

    nixie_toolchain = step_named(
        workflow_data, "lint-test", "Install Rust toolchain for Nixie"
    )
    project_toolchain = step_named(
        workflow_data, "lint-test", "Install project Rust toolchain"
    )
    assert nixie_toolchain.get("with") == {
        "toolchain": "1.95.0",
        "cache-provider": "external",
        "use-sccache": "false",
    }
    assert project_toolchain.get("with") == {
        "toolchain": "1.85.0",
        "cache-provider": "external",
        "use-sccache": "false",
    }
    whitaker_installer = step_named(workflow_data, "lint-test", "Install Whitaker")
    assert whitaker_installer.get("with") == {
        "installer-version": "${{ env.WHITAKER_INSTALLER_VERSION }}"
    }

    lint_environment = job(workflow_data, "lint-test").get("env")
    assert isinstance(lint_environment, dict), "lint-test must declare an environment"
    assert lint_environment.get("WHITAKER_INSTALLER_VERSION") == "0.2.7"

    step_names = [step.get("name") for step in steps(workflow_data, "lint-test")]
    nixie_gate, _ = first_step_running(
        workflow_data, "make nixie", job_name="lint-test"
    )
    assert (
        step_names.index("Install Rust toolchain for Nixie")
        < step_names.index("Install Nixie")
        < step_names.index("Install project Rust toolchain")
        < step_names.index("Install Whitaker")
        < nixie_gate
    )
    assert "Cache Whitaker installation" not in step_names

    legacy_whitaker_commands = [
        script
        for job_name, script in run_scripts(workflow_data)
        if job_name == "lint-test"
        and ("cargo binstall" in script or "whitaker-installer" in script)
    ]
    assert not legacy_whitaker_commands, (
        "lint-test must install Whitaker through the shared action, not "
        f"manual commands: {legacy_whitaker_commands}"
    )


def test_workflows_pin_shared_actions_to_immutable_revisions() -> None:
    """Every shared-actions caller uses an immutable revision without fixing it."""
    all_uses_by_workflow = {
        workflow_name: _shared_action_uses(workflow_document(workflow_name))
        for workflow_name, _ in workflow_sources()
    }
    uses_by_workflow = {
        workflow_name: uses
        for workflow_name, uses in all_uses_by_workflow.items()
        if uses
    }
    parsed_references = {
        workflow_name: [
            (uses, SHARED_ACTIONS_REFERENCE.fullmatch(uses)) for uses in uses_values
        ]
        for workflow_name, uses_values in uses_by_workflow.items()
    }
    invalid_references = [
        f"{workflow_name}:{uses}"
        for workflow_name, references in parsed_references.items()
        for uses, reference in references
        if reference is None
    ]
    assert not invalid_references, (
        "shared-actions references must use a full lowercase 40-character "
        f"commit SHA after '@': {invalid_references}"
    )
    valid_references = {
        workflow_name: [
            reference for _, reference in references if reference is not None
        ]
        for workflow_name, references in parsed_references.items()
    }

    targets_by_workflow = {
        workflow_name: {reference["target"] for reference in references}
        for workflow_name, references in valid_references.items()
    }
    assert targets_by_workflow == EXPECTED_SHARED_ACTIONS_TARGETS, (
        "each workflow must call only its expected shared actions or reusable "
        f"workflows; found {targets_by_workflow}"
    )
