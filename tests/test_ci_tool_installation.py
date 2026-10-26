"""Contract tests for how Cuprum's CI installs its tools.

Every tool a gate runs is fetched from a pinned, prebuilt, checksum-verified
path. A source build is slow, unpinned in its dependencies, and fails against a
pinned toolchain in ways no code change explains; and an installer for a tool
nothing runs is a download whose failure mode nothing exercises.
"""

from __future__ import annotations

import re
import typing as typ

import pytest

from tests.helpers.ci_placement import declares_steps
from tests.helpers.ci_runners import (
    GENERATE_COVERAGE,
    steps,
    workflow_document,
    workflow_sources,
)

if typ.TYPE_CHECKING:
    from tests.helpers.workflow_types import Step

#: Any spelling of the tool, so `nextest@` and `cargo-nextest@` both match.
NEXTEST_TOOL_NAME = "nextest"
#: Shell fragments that mean "fetch a binary" rather than "run one".
INSTALL_VERBS = ("install", "curl", "wget")
#: The upstream install host. Its name does not contain "nextest", so it needs
#: its own token, and it only ever appears in an install command.
NEXTEST_INSTALL_HOST = "get.nexte.st"
#: The one sanctioned installer, matched exactly. A prefix over the whole
#: shared-actions namespace would exempt setup-rust, install-mdtablefix and
#: every sibling, so any of them could take a nextest input unremarked.


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


def test_only_modern_stable_installs_cargo_nextest() -> None:
    """Use one pinned prebuilt installer for the modern validation job.

    The ordinary Rust matrix no longer runs the Rust suite, so nextest belongs
    only on the one modern-stable job that invokes it. That route must install
    an official checksum-verified binary and never build the tool from source.

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
    assert offenders == [
        "ci.yml:modern-stable:Install pinned prebuilt cargo-nextest"
    ], f"only the modern stable job may install cargo-nextest, found {offenders}"


def test_modern_stable_installs_the_pinned_nextest_release_from_checksums() -> None:
    """Verify nextest is a cached-or-prebuilt binary, never a Cargo build."""
    step = next(
        step
        for step in steps("ci.yml", "modern-stable")
        if step.get("name") == "Install pinned prebuilt cargo-nextest"
    )
    script = step.get("run")
    assert isinstance(script, str), "the nextest installer must run a shell script"
    version = re.search(r"(?m)^\s*readonly CARGO_NEXTEST_VERSION='([^']+)'", script)
    checksums = dict(
        re.findall(
            r"(?m)^\s*readonly CARGO_NEXTEST_(ARCHIVE|BINARY)_SHA256='([0-9a-f]+)'",
            script,
        )
    )
    assert version is not None, "the nextest installer must declare its version"
    assert version.group(1) == "0.9.120", (
        "the nextest installer must pin the reviewed binary release"
    )
    assert set(checksums) == {"ARCHIVE", "BINARY"}, (
        "the nextest installer must verify both archive and binary checksums"
    )
    assert checksums.get("ARCHIVE") == (
        "a5b1c12500c47e27af4baf533c917bf1b38e9bf2e6ffb063dfa1de6e75aa8726"
    ), "the nextest archive must retain its reviewed checksum"
    assert checksums.get("BINARY") == (
        "8d717594668f0ec817405b9526cb657ca40fc888068277004860d0f253837d14"
    ), "the nextest binary must retain its reviewed checksum"
    archive_check = script.index('"${CARGO_NEXTEST_ARCHIVE_SHA256}"')
    assert archive_check < script.index("tar --extract"), (
        "the downloaded nextest archive must be verified before extraction"
    )
    assert "cargo install" not in script, (
        "CI must use the prebuilt nextest binary rather than source-building it"
    )


def _installs_nextest_by_input(step: Step) -> bool:
    """Report whether a step asks an installer action for cargo-nextest."""
    if str(step.get("uses", "")) == GENERATE_COVERAGE:
        # The coverage action installs nextest deliberately; that is the one
        # place it is meant to happen, and it is matched exactly rather than by
        # namespace so no sibling action inherits the exemption.
        return False
    inputs = step.get("with")
    if not isinstance(inputs, dict):
        return False
    return any(NEXTEST_TOOL_NAME in str(value).lower() for value in inputs.values())


def _installs_nextest_by_script(step: Step) -> bool:
    """Report whether a step's script fetches cargo-nextest rather than runs it."""
    script = str(step.get("run", "")).lower()
    named = NEXTEST_TOOL_NAME in script and any(
        verb in script for verb in INSTALL_VERBS
    )
    # The install host needs its own token: its name does not contain
    # "nextest", and it only ever appears in an install command.
    return named or NEXTEST_INSTALL_HOST in script


def _nextest_installers(workflow_name: str, job_name: str) -> list[str]:
    """Return descriptions of steps in one job that would install nextest."""
    if not declares_steps(workflow_name, job_name):
        return []
    return [
        f"{workflow_name}:{job_name}:{step.get('name') or step.get('uses')}"
        for step in steps(workflow_name, job_name)
        if _installs_nextest_by_input(step) or _installs_nextest_by_script(step)
    ]


def test_markdown_lint_runs_through_the_pinned_action() -> None:
    """Keep the Markdown linter on a reproducible, SHA-pinned release.

    The estate's markdown-formatting-baseline rule requires CI to lint
    Markdown through the upstream markdownlint-cli2 action rather than a
    shell install, so the pin lives on the action reference.
    """
    lint_steps = [
        step
        for step in steps("ci.yml", "lint-test")
        if str(step.get("uses", "")).startswith("DavidAnson/markdownlint-cli2-action@")
    ]
    assert len(lint_steps) == 1, (
        "ci.yml:lint-test must lint Markdown with the action once"
    )
    reference = str(lint_steps[0]["uses"]).split("@", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{40}", reference), (
        "ci.yml:lint-test must pin the markdownlint-cli2 action to a full SHA"
    )
    inputs = lint_steps[0].get("with")
    assert isinstance(inputs, dict), "ci.yml:lint-test action step must carry inputs"
    assert str(inputs.get("globs", "")).splitlines() == [
        "**/*.md",
        "**/*.markdown",
        "**/*.mdx",
    ], "ci.yml:lint-test must lint every supported Markdown extension"
    script = next(
        step["run"]
        for step in steps("ci.yml", "lint-test")
        if step.get("name") == "Install CLI tools"
    )
    assert isinstance(script, str), "ci.yml:Install CLI tools must run a script"
    assert "markdownlint-cli2" not in script, (
        "ci.yml:Install CLI tools must not install markdownlint-cli2 from npm"
    )


@pytest.mark.parametrize(
    ("uses", "reported"),
    [
        pytest.param(GENERATE_COVERAGE, False, id="the-sanctioned-action"),
        pytest.param(
            "leynos/shared-actions/.github/actions/setup-rust@" + "0" * 40,
            True,
            id="a-sibling-shared-action",
        ),
        pytest.param("actions/checkout@" + "0" * 40, True, id="a-third-party-action"),
    ],
)
def test_only_the_coverage_action_may_ask_for_nextest(
    uses: str, *, reported: bool
) -> None:
    """Restrict the exemption to the one action that installs nextest on purpose.

    Driven directly because no workflow here gives a sibling action a nextest
    input, so a contract read over the real files passes whether the exemption
    is exact or spans the whole namespace. Widening it back to a prefix
    survived every test until this one existed.
    """
    step = typ.cast("Step", {"uses": uses, "with": {"tool": "cargo-nextest@0.9.0"}})
    assert _installs_nextest_by_input(step) is reported, (
        f"{uses} must {'be' if reported else 'not be'} reported as installing nextest"
    )
