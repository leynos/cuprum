"""Contracts for installing the Makefile parser through the shared action.

`makeutil` parses the Makefile for the contract tests. CI installs its prebuilt
release binary with the shared `install-makeutil` action, pinned by commit, and
never compiles it or downloads it by hand. The action owns the cache of its own
binary and re-verifies a restored one against its pinned digest, so this
repository's tool cache neither carries the parser nor skips its install. The
contracts hold what that arrangement needs:

* every job that runs the Makefile contracts installs through the action, with
  no `run` key and only a `bin-dir` input, so the version is the action's own
  default and the binary stays out of the tool cache;
* the next step verifies the install, comparing the binary's version with the
  one the action reports and requiring a complete parse of the `Makefile`; and
* nothing else in the workflows or local actions fetches or builds makeutil,
  and no job restates a pin.

The step shapes live in `tests/helpers/ci_makeutil.py`.
"""

from __future__ import annotations

import re
import typing as typ
from pathlib import Path

import pytest

from tests.helpers.ci_makeutil import (
    INSTALL_ID,
    INSTALL_STEP,
    VERIFY_STEP,
    assert_installation,
    assert_verification,
    assert_verification_follows_install,
)
from tests.helpers.ci_workflows import ROOT, job_env, steps, workflow_sources

if typ.TYPE_CHECKING:
    from tests.helpers.workflow_types import Step

#: The makeutil source, which only the shared action may name.
MAKEUTIL_SOURCE_URL: typ.Final = "https://github.com/leynos/makeutil"
#: What a retired local action or pin variable would have left behind.
RETIRED_PIN_KEYS: typ.Final = (
    "MAKEUTIL_VERSION",
    "MAKEUTIL_TARGET",
    "MAKEUTIL_SHA256",
)
#: The local action this repository retired in favour of the shared one.
RETIRED_LOCAL_ACTION: typ.Final = ".github/actions/install-makeutil"

#: Every job that runs the Makefile contracts, and so needs the parser.
CONSUMERS: typ.Final = (
    ("ci.yml", "typecheck-test"),
    ("ci.yml", "coverage"),
    ("coverage-main.yml", "coverage-upload"),
)


def _only(job_steps: list[Step], name: str, *, where: str) -> Step:
    """Return the one step called ``name``."""
    matches = [step for step in job_steps if step.get("name") == name]
    assert len(matches) == 1, f"{where} must have exactly one {name!r} step"
    return matches[0]


@pytest.mark.parametrize(("workflow_name", "job_name"), CONSUMERS)
def test_a_consumer_installs_through_the_shared_action(
    workflow_name: str, job_name: str
) -> None:
    """The install step is the pinned action, `bin-dir` only, with an id."""
    where = f"{workflow_name}:{job_name}"
    step = _only(steps(workflow_name, job_name), INSTALL_STEP, where=where)
    assert_installation(workflow_name, job_name, step, contract=where)


@pytest.mark.parametrize(("workflow_name", "job_name"), CONSUMERS)
def test_a_consumer_verifies_the_install_straight_away(
    workflow_name: str, job_name: str
) -> None:
    """The verify step compares versions and parses the Makefile, never naming one."""
    job_steps = steps(workflow_name, job_name)
    where = f"{workflow_name}:{job_name}"
    verify = _only(job_steps, VERIFY_STEP, where=where)
    assert_verification(workflow_name, job_name, verify, contract=where)
    assert_verification_follows_install(job_steps, contract=where)


@pytest.mark.parametrize(("workflow_name", "job_name"), CONSUMERS)
def test_no_consumer_restates_a_pin(workflow_name: str, job_name: str) -> None:
    """A job-level copy of a pin would read as the pin while pinning nothing."""
    restated = sorted(set(RETIRED_PIN_KEYS) & set(job_env(workflow_name, job_name)))
    assert restated == [], f"{workflow_name}:{job_name} restates {restated}"


def test_the_install_id_is_unique_in_each_consumer() -> None:
    """The verify step reads the install step's id, so a second one would shadow it."""
    for workflow_name, job_name in CONSUMERS:
        ids = [
            step.get("id")
            for step in steps(workflow_name, job_name)
            if step.get("id") == INSTALL_ID
        ]
        assert ids == [INSTALL_ID], f"{workflow_name}:{job_name} ids: {ids}"


#: A command that builds or installs makeutil by package name rather than by
#: URL: `cargo install`, `cargo +toolchain install`, and either binstall form.
_PACKAGE_INSTALL = re.compile(
    r"(cargo(\s+\+\S+)?\s+b?install|cargo-binstall)\b[^\n]*\bmakeutil\b"
)


def _installs_makeutil_elsewhere(source: str) -> bool:
    """Return whether ``source`` fetches or installs makeutil by any route."""
    joined = source.replace("\\\n", " ")
    return MAKEUTIL_SOURCE_URL in joined or bool(_PACKAGE_INSTALL.search(joined))


def _local_actions() -> list[tuple[str, str]]:
    """Return every local composite action's name and source."""
    return [
        (str(path.relative_to(ROOT)), path.read_text(encoding="utf-8"))
        for path in sorted(Path(ROOT, ".github", "actions").glob("*/action.y*ml"))
    ]


def test_nothing_in_ci_fetches_makeutil_itself() -> None:
    """A fetch or build outside the shared action would carry an unchecked pin.

    Both routes count: a fetch of the repository URL, and a package install
    such as `cargo install makeutil`, which names no URL at all.
    """
    builders = [
        name
        for name, source in [*workflow_sources(), *_local_actions()]
        if _installs_makeutil_elsewhere(source)
    ]
    assert builders == [], f"only the shared action may fetch makeutil: {builders}"


def test_the_retired_local_action_is_gone_and_unreferenced() -> None:
    """A leftover local action, or a `uses:` of it, would be a second install path."""
    assert not (ROOT / RETIRED_LOCAL_ACTION).exists(), (
        f"{RETIRED_LOCAL_ACTION} was retired for the shared action"
    )
    users = [
        name
        for name, source in workflow_sources()
        if f"./{RETIRED_LOCAL_ACTION}" in source
    ]
    assert users == [], f"workflows still use the retired local action: {users}"


@pytest.mark.parametrize(
    "command",
    [
        "cargo install makeutil",
        "cargo +nightly-2026-05-28 install --locked makeutil",
        "cargo binstall makeutil",
        "cargo-binstall --no-confirm makeutil",
        "cargo install \\\n  --locked makeutil",
        f"curl -o m {MAKEUTIL_SOURCE_URL}/releases/download/v1/makeutil",
    ],
)
def test_the_refusal_recognizes_every_install_route(command: str) -> None:
    """Each route is found, so the refusal above cannot pass by missing one."""
    assert _installs_makeutil_elsewhere(command), command


def test_the_refusal_ignores_other_tools() -> None:
    """Installing a different crate is not a makeutil install."""
    assert not _installs_makeutil_elsewhere("cargo install cargo-nextest --locked"), (
        "installing another crate must not read as a makeutil install"
    )
