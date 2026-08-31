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

import pytest

from tests.helpers.workflow import (
    first_step_running,
    run_scripts,
    script_runs_command,
    workflow,
)

WORKFLOW_DATA = workflow()


def test_the_ci_job_builds_the_extension_before_running_the_gated_tests() -> None:
    """`extension-tests` must run `make develop` first.

    `make build` only syncs dependencies, so this ordering is the whole reason
    the job can pass at all, and nothing else asserts it.
    """
    build, _ = first_step_running(
        WORKFLOW_DATA, "make develop", job_name="extension-tests"
    )
    tests, _ = first_step_running(
        WORKFLOW_DATA, "make test-extension", job_name="extension-tests"
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
        ("if make develop", "make develop", True),
        ("then make develop", "make develop", True),
        ("elif make develop", "make develop", True),
        ("else make develop", "make develop", True),
        ("do make develop", "make develop", True),
        ("# make develop", "make develop", False),
        ('echo "make develop"', "make develop", False),
        ("echo if make develop", "make develop", False),
        ("cat <<EOF\nif make develop\nEOF", "make develop", False),
        ("cat <<-EOF\n\tif make develop\n\tEOF\nmake develop", "make develop", True),
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


def test_only_boundary_jobs_build_the_extension() -> None:
    """Only jobs isolated from the full suite may install the extension."""
    builders = {
        job_name
        for job_name, script in run_scripts(WORKFLOW_DATA)
        if script_runs_command(script, "make develop")
    }

    assert builders == {"benchmark-ratchet", "extension-tests"}, (
        "only the isolated extension and benchmark jobs may run `make develop`; "
        f"found {builders}"
    )


def test_the_benchmark_job_builds_through_the_develop_target() -> None:
    """`benchmark-ratchet` must reach an optimized build via `make develop`.

    Its numbers mean nothing against a debug build, so the flag matters
    as much as the shared target does.
    """
    _, script = first_step_running(
        WORKFLOW_DATA, "make develop", job_name="benchmark-ratchet"
    )

    assert "make develop MATURIN_DEVELOP_FLAGS=--release" in script, (
        "the benchmark-ratchet job must build with `make develop "
        "MATURIN_DEVELOP_FLAGS=--release`; without the flag the ratchet "
        "compares debug builds and its thresholds mean nothing"
    )


def test_no_ci_step_invokes_maturin_develop_directly() -> None:
    """`make develop` must be the only definition of the extension build.

    A second copy of the three-step sequence is how the two drift: the copy
    stops matching the target, and whichever job owns it quietly builds
    something nobody maintains.
    """
    offenders = sorted({
        job_name
        for job_name, script in run_scripts(WORKFLOW_DATA)
        if script_runs_command(script, "maturin develop")
    })

    assert not offenders, (
        "these CI jobs invoke `maturin develop` directly instead of going "
        f"through `make develop`: {offenders}"
    )
