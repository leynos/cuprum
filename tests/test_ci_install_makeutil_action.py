"""Execute the install-makeutil action's step against stand-in commands.

`tests/test_ci_makeutil_install.py` holds the step's text. These tests run it:
the step's own ``run`` body executes under ``bash`` with ``git``, ``rustup``
and ``cargo`` replaced by commands that record their arguments and fail on
request. That shows what the text cannot: that a pin makeutil's ``main`` does
not reach stops the step before any toolchain or compile, that a failed
toolchain install or compile fails the step, and that the build receives the
pinned revision, toolchain and Polonius flag.

Nothing touches the network or a real toolchain.
"""

from __future__ import annotations

import os
import typing as typ

import pytest

from tests.helpers.composite_actions import action_document, run_step, step_script

if typ.TYPE_CHECKING:
    from pathlib import Path

ACTION: typ.Final = ".github/actions/install-makeutil"
STEP: typ.Final = "Build makeutil at the pinned revision"
#: A stand-in shared by all three commands: it logs its name, its arguments
#: and `RUSTFLAGS`, then fails when its name and arguments contain `FAIL_ON`.
STAND_IN: typ.Final = """#!/bin/bash
line="$(basename "$0") $*"
printf '%s | RUSTFLAGS=%s\\n' "${line}" "${RUSTFLAGS:-}" >> "${CALL_LOG}"
if [ -n "${FAIL_ON:-}" ] && [[ "${line}" == *"${FAIL_ON}"* ]]; then
  exit 1
fi
"""


def _pin() -> dict[str, str]:
    """Return the step's declared ``env``, which holds the pin."""
    runs = typ.cast("dict[str, object]", action_document(ACTION)["runs"])
    step = typ.cast("list[dict[str, object]]", runs["steps"])[0]
    return typ.cast("dict[str, str]", step["env"])


def _run(tmp_path: Path, fail_on: str = "") -> tuple[int, list[str]]:
    """Run the step with stand-in commands; return its status and the calls."""
    commands = tmp_path / "commands"
    commands.mkdir()
    for name in ("git", "rustup", "cargo"):
        program = commands / name
        program.write_text(STAND_IN, encoding="utf-8")
        program.chmod(0o755)
    log = tmp_path / "calls"
    log.touch()
    result = run_step(
        step_script(ACTION, STEP),
        workdir=tmp_path,
        environment={
            "PATH": f"{commands}:{os.environ['PATH']}",
            "CALL_LOG": str(log),
            "FAIL_ON": fail_on,
            **_pin(),
        },
    )
    return result.returncode, log.read_text(encoding="utf-8").splitlines()


def _program(call: str) -> str:
    """Return the command name a logged call starts with."""
    return call.split(" ", 1)[0]


def test_a_reachable_pin_is_checked_then_built(tmp_path: Path) -> None:
    """The ancestry check runs first, then the toolchain, then the pinned build."""
    status, calls = _run(tmp_path)
    pin = _pin()
    assert status == 0, calls
    assert [_program(call) for call in calls] == [
        "git",
        "git",
        "git",
        "rustup",
        "cargo",
    ], calls
    assert "refs/heads/main" in calls[1], calls[1]
    assert (
        f"merge-base --is-ancestor {pin['MAKEUTIL_REVISION']} FETCH_HEAD" in calls[2]
    ), calls[2]
    assert f"toolchain install {pin['MAKEUTIL_TOOLCHAIN']}" in calls[3], calls[3]
    build = calls[4]
    assert f"+{pin['MAKEUTIL_TOOLCHAIN']} install" in build, build
    assert f"--rev {pin['MAKEUTIL_REVISION']}" in build, build
    assert build.endswith("| RUSTFLAGS=-Zpolonius=next"), build


@pytest.mark.parametrize(
    ("fail_on", "last_program"),
    [
        ("merge-base", "git"),
        ("fetch", "git"),
        ("rustup toolchain install", "rustup"),
        ("cargo +", "cargo"),
    ],
    ids=["unreachable pin", "history fetch", "toolchain install", "compile"],
)
def test_a_failure_stops_the_step_where_it_happens(
    tmp_path: Path, fail_on: str, last_program: str
) -> None:
    """Each failure fails the step, and nothing after it runs.

    An unreachable pin in particular must never reach the build: a commit no
    branch reaches builds until GitHub garbage-collects it.
    """
    status, calls = _run(tmp_path, fail_on=fail_on)
    assert status != 0, f"the step must fail when {fail_on!r} fails: {calls}"
    assert calls, "the step must have run something"
    assert fail_on in calls[-1], f"nothing may run after {fail_on!r}: {calls}"
    assert _program(calls[-1]) == last_program, calls
