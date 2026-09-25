"""Contracts for installing the Makefile parser from the tool cache.

`makeutil` parses the Makefile for the contract tests. CI downloads a pinned
release binary and checks it against a digest pinned beside the version; it no
longer compiles makeutil. The binary lands in `~/.cargo/bin`, which the tool
cache already carries, so a job whose tool cache hit exactly already holds it.
Skipping the download on that hit is safe only while the hit implies the pin,
and these contracts hold the three things that make it so:

* the pin lives in one place, `.github/actions/install-makeutil`, and nothing
  else in the workflows fetches or builds makeutil;
* the tool family's key hashes that action, so changing the pin misses and
  downloads rather than keeping a stale binary; and
* every consumer restores the tool cache, with `~/.cargo/bin` in it, before an
  install step that runs through the action and only on a miss.
"""

from __future__ import annotations

import re
import shlex
import typing as typ

import pytest

from tests.helpers.ci_leg_gate import ungated
from tests.helpers.ci_runners import CACHE_KEYS_ACTION_FILE
from tests.helpers.ci_workflows import (
    ROOT,
    cache_paths,
    job_env,
    read_workflow,
    steps,
    workflow_sources,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from tests.helpers.workflow_types import Step

INSTALL_ACTION: typ.Final = "./.github/actions/install-makeutil"
INSTALL_ACTION_PATH: typ.Final = ".github/actions/install-makeutil/action.yml"
INSTALL_STEP: typ.Final = "Install Makefile parser"
TOOL_CACHE_ID: typ.Final = "tool-cache"
#: The install step's whole guard: run only when the tool cache missed.
MISS_GUARD: typ.Final = f"steps.{TOOL_CACHE_ID}.outputs.cache-hit != 'true'"
#: Where the action installs the binary, and so what the tool cache must hold.
CARGO_BIN: typ.Final = "~/.cargo/bin"
#: The makeutil source, which only the action may name.
MAKEUTIL_SOURCE_URL: typ.Final = "https://github.com/leynos/makeutil"

#: The pin, as the action declares it.
PIN: typ.Final = {
    "MAKEUTIL_VERSION": "v0.1.0",
    "MAKEUTIL_TARGET": "x86_64-unknown-linux-musl",
    "MAKEUTIL_SHA256": (
        "99dd28a138dbe07e88e4dc5dd3954e6b29b46cc959635311d326cb537253115d"
    ),
}
#: The action's whole command, tokenized: download into a scratch directory,
#: check the pinned digest, and only then install.
INSTALL_TOKENS: typ.Final = (
    "set",
    "-euo",
    "pipefail",
    "download=$(mktemp -d)/makeutil",
    "curl",
    "--fail",
    "--location",
    "--show-error",
    "--silent",
    "--output",
    "${download}",
    (
        f"{MAKEUTIL_SOURCE_URL}/releases/download/${{MAKEUTIL_VERSION}}"
        "/makeutil-${MAKEUTIL_TARGET}"
    ),
    "printf",
    "%s  %s\\n",
    "${MAKEUTIL_SHA256}",
    "${download}",
    "|",
    "sha256sum",
    "--check",
    "--status",
    "install",
    "-D",
    "--mode=0755",
    "${download}",
    "${HOME}/.cargo/bin/makeutil",
)

#: Every job that runs the Makefile contracts, and so needs the parser.
CONSUMERS: typ.Final = (
    ("ci.yml", "typecheck-test"),
    ("ci.yml", "coverage"),
    ("coverage-main.yml", "coverage-upload"),
)


def _action_step() -> Step:
    """Return the install action's one step."""
    action = read_workflow(ROOT / INSTALL_ACTION_PATH)
    runs = action.get("runs")
    assert isinstance(runs, dict), f"{INSTALL_ACTION_PATH} must declare runs"
    action_steps = typ.cast("dict[str, object]", runs).get("steps")
    assert isinstance(action_steps, list), f"{INSTALL_ACTION_PATH} must have steps"
    assert len(action_steps) == 1, f"{INSTALL_ACTION_PATH} must have one step"
    return typ.cast("Step", action_steps[0])


def _position(
    job_steps: list[Step], *, what: str, predicate: cabc.Callable[[Step], bool]
) -> int:
    """Return the index of the one step matching ``predicate``."""
    matches = [index for index, step in enumerate(job_steps) if predicate(step)]
    assert len(matches) == 1, f"expected one {what}, found {len(matches)}"
    return matches[0]


def test_the_action_installs_the_checked_release() -> None:
    """The action's one step is the checked download, with the pin in its ``env``."""
    step = _action_step()
    assert step.get("env") == PIN, (
        f"{INSTALL_ACTION_PATH} must pin {PIN}, got {step.get('env')!r}"
    )
    command = step.get("run")
    assert isinstance(command, str), f"{INSTALL_ACTION_PATH} must run a command"
    assert tuple(shlex.split(command.replace("\\\n", ""))) == INSTALL_TOKENS, (
        f"{INSTALL_ACTION_PATH} must run exactly the checked download, got {command!r}"
    )


def test_the_pin_is_a_release_and_a_full_digest() -> None:
    """A floating version or a short digest could admit a different binary.

    The pin is read from the action rather than from ``PIN``.
    """
    environment = _action_step().get("env")
    assert isinstance(environment, dict), f"{INSTALL_ACTION_PATH} must pin in env"
    pin = typ.cast("dict[str, object]", environment)
    version = str(pin.get("MAKEUTIL_VERSION"))
    digest = str(pin.get("MAKEUTIL_SHA256"))
    assert re.fullmatch(r"v\d+\.\d+\.\d+", version), (
        f"the makeutil pin must be an exact release, got {version!r}"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", digest), (
        f"the makeutil digest must be a full SHA-256, got {digest!r}"
    )


def test_the_tool_key_hashes_the_pin() -> None:
    """Without this, a changed pin would hit the old key and skip the rebuild."""
    source = CACHE_KEYS_ACTION_FILE.read_text(encoding="utf-8")
    assert f"'{INSTALL_ACTION_PATH}'" in source, (
        f"the tool key's hashFiles must include {INSTALL_ACTION_PATH}"
    )


def test_only_the_action_fetches_makeutil() -> None:
    """A second fetch or build elsewhere would carry a pin the key does not hash."""
    builders = [
        name for name, source in workflow_sources() if MAKEUTIL_SOURCE_URL in source
    ]
    assert builders == [], f"only {INSTALL_ACTION_PATH} may fetch makeutil: {builders}"


@pytest.mark.parametrize(("workflow_name", "job_name"), CONSUMERS)
def test_a_consumer_installs_through_the_action_only_on_a_tool_miss(
    workflow_name: str, job_name: str
) -> None:
    """The install runs the action, on a miss, after the restore that decides it.

    A guard reading a step that runs later would always see an empty output,
    so the install would run every time and save nothing.
    """
    job_steps = steps(workflow_name, job_name)
    where = f"{workflow_name}:{job_name}"
    install = _position(
        job_steps,
        what=f"{where} {INSTALL_STEP!r} step",
        predicate=lambda step: step.get("name") == INSTALL_STEP,
    )
    restore = _position(
        job_steps,
        what=f"{where} step with id {TOOL_CACHE_ID!r}",
        predicate=lambda step: step.get("id") == TOOL_CACHE_ID,
    )
    step = job_steps[install]
    assert step.get("uses") == INSTALL_ACTION, (
        f"{where} must install through {INSTALL_ACTION}, got {step.get('uses')!r}"
    )
    # `typecheck-test` also ends every guard with its leg flag; `ungated`
    # requires that conjunct there and removes it, and leaves other jobs' guards
    # whole.
    guard = ungated(workflow_name, job_name, step.get("if"))
    assert guard == MISS_GUARD, f"{where} install must be guarded {MISS_GUARD!r}"
    assert restore < install, f"{where} must restore the tool cache before install"
    assert CARGO_BIN in cache_paths(job_steps[restore], f"{where} tool cache"), (
        f"{where}'s tool cache must carry {CARGO_BIN}, where makeutil is installed"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), CONSUMERS)
def test_no_consumer_restates_the_pin(workflow_name: str, job_name: str) -> None:
    """A job-level copy of the pin would read as the pin while pinning nothing."""
    restated = sorted(set(PIN) & set(job_env(workflow_name, job_name)))
    assert restated == [], f"{workflow_name}:{job_name} restates {restated}"
