"""Contract for the version check that guards the nextest timeout tiers.

`rust/.config/nextest.toml` declares `global-timeout`, and nextest warns
about a configuration key it does not recognize and carries on. On a
release that predates that key the whole-run budget is therefore dropped
silently while the suite still passes. The `Makefile` refuses to run
nextest below `NEXTEST_MIN_VERSION` so that cannot happen, and this module
holds the check to the properties that make the refusal real. The
agreement between the config's declaration and the Makefile's floor is
`test_the_nextest_floor_agrees_between_the_config_and_the_makefile`, which
owns that comparison; this module is about the check itself.

The first property is that the check and the binary it guards resolve
against *one* environment. Each call site probes with `LOCAL_TOOL_ENV`'s
augmented `PATH`, which is what finds a tool in `~/.local/bin` on a host
whose ambient `PATH` lacks that directory. A version call left on the
ambient `PATH` then fails with 127 and prints nothing, so the probe finds
a tool the check cannot run and the floor is never enforced for it.

The second is that empty input is refused rather than accepted. A version
check whose subject never arrived has nothing to certify: a comparison of
no version against the floor reads as "not below" it and certifies every
release, including the ones the floor exists to refuse.

The `Makefile` is read as text for the environment property, because the
recipes only run where a real nextest and toolchain are installed. The awk
program is extracted and exercised directly instead, so its verdicts are
checked without either, and a wrong verdict fails here rather than in a
gate run that silently dropped a tier.
"""

from __future__ import annotations

import re
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, reads the local Makefile.
import typing as typ

import pytest

from cuprum.unittests._timeout_lane_support import EXPECTED_NEXTEST_MIN_VERSION
from tests.helpers.docs import repo_root

#: The Makefile variable holding the version check, and the target whose
#: recipe carries a call site. The variable is read for the environment
#: property and to name the definition; the recipe is read for the program,
#: because only there has Make collapsed `$$` to `$`.
_CHECK_VARIABLE = "NEXTEST_VERSION_OK"
_CHECK_TARGET = "test-rust"

#: Commands that must carry the environment the probe resolved against.
#: `nextest run` is a Cargo subcommand, so it inherits the environment
#: from the `cargo` invocation on its own line rather than carrying one.
_GUARDED_COMMANDS: typ.Final[tuple[str, ...]] = (
    "command -v cargo-nextest",
    "cargo-nextest --version",
)

#: A release below the floor, one at it, and ones above it, plus the shape
#: a real `cargo-nextest --version` line takes. The last is here because
#: the version is found by shape rather than position: `cargo-nextest
#: 0.9.133 (65e806bd5 2026-04-14)` puts it in the second field, so a
#: program reading a fixed position would see the commit hash instead.
_VERSION_PROBES: typ.Final[tuple[tuple[str, int], ...]] = (
    ("0.9.54", 1),
    ("0.9.99", 1),
    ("0.9.100", 0),
    ("0.9.120", 0),
    ("0.9.133", 0),
    ("0.10.0", 0),
    ("1.0.0", 0),
    ("cargo-nextest 0.9.133 (65e806bd5 2026-04-14)", 0),
)


def _makefile_text() -> str:
    """Return the repository Makefile's text."""
    return (repo_root() / "Makefile").read_text(encoding="utf-8")


def _awk_program() -> str:
    """Return the awk program the version check actually runs.

    Read from the *expanded* recipe rather than from the variable's text in
    the file, because the two differ in a way that changes the verdict.
    `$$i` in the Makefile is an escaped `$i`: Make collapses each `$$` to
    one `$` before the shell sees it, and awk's `$i` is the field. Reading
    the file's text directly would hand awk `$$i`, which is `$($i)` -- a
    second indirection through the *value* of field `i`. That happens to
    agree on single-field input, where `$($1)` is `$0` and so the same
    string, and disagrees on the input the check exists for: a real
    `cargo-nextest --version` line has four fields, so `$($2)` collapses to
    the whole line and the version field is never examined.

    The floor reaches the program through `-v min=` rather than by editing
    its text, so the bound is supplied separately.

    Returns
    -------
    str
        The program text, with Make's escapes already resolved.
    """
    make = shutil.which("make")
    assert make is not None, "the version check lives in a Makefile recipe"
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell needed.
        [make, "--dry-run", _CHECK_TARGET],
        capture_output=True,
        check=True,
        cwd=repo_root(),
        text=True,
    )
    line = next(
        (
            expanded
            for expanded in completed.stdout.splitlines()
            if "awk -v min=" in expanded and _CHECK_VARIABLE not in expanded
        ),
        None,
    )
    assert line is not None, (
        f"the {_CHECK_TARGET} recipe must expand to an awk version check; a "
        f"check that no target runs enforces no floor"
    )
    remainder = re.search(r'awk -v min="[^"]*" (.*)', line)
    assert remainder is not None, (
        f"the version check must bind the floor through `-v min=`, not by "
        f"editing the program: {line.strip()}"
    )
    quoted = remainder.group(1)
    start = quoted.index("'")
    end = quoted.index("'", start + 1)
    return quoted[start + 1 : end]


def _awk_verdict(stdin: str) -> int:
    """Run the extracted check over `stdin` and return its exit status."""
    awk = shutil.which("awk")
    assert awk is not None, "the version check is an awk program"
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell needed.
        [awk, "-v", f"min={EXPECTED_NEXTEST_MIN_VERSION}", _awk_program()],
        input=stdin,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode in {0, 1}, (
        f"the version check must exit 0 or 1, not {completed.returncode}: "
        f"{completed.stderr.strip()}"
    )
    return completed.returncode


@pytest.mark.parametrize(("version", "expected"), _VERSION_PROBES)
def test_the_check_orders_a_release_against_the_floor(
    version: str, expected: int
) -> None:
    """A release below the floor is refused and one at or above it runs."""
    verdict = _awk_verdict(f"{version}\n")
    assert verdict == expected, (
        f"the version check returned {verdict} for {version!r} against a "
        f"{EXPECTED_NEXTEST_MIN_VERSION} floor, not {expected}; a release below "
        f"the floor that is accepted runs the suite with the whole-run budget "
        f"dropped, and one above it that is refused blocks a healthy run"
    )


def test_the_check_refuses_empty_input() -> None:
    """A check whose subject never arrived must not certify a release.

    Empty input is what a version call that could not run produces: a
    `cargo-nextest` resolved by the probe but not by the call exits 127 and
    prints nothing, and a comparison of no version against the floor reads
    as "not below" it. Without the guard the check passes for every
    release, and for none.
    """
    verdict = _awk_verdict("")
    assert verdict == 1, (
        "the version check accepted empty input, but an empty stream is what a "
        "version call that could not run produces; accepting it certifies "
        "every release, including the ones the floor exists to refuse"
    )


@pytest.mark.parametrize("command", _GUARDED_COMMANDS)
def test_every_nextest_command_carries_the_probe_environment(command: str) -> None:
    """Each call site must run its nextest commands under one environment.

    The probe resolves `cargo-nextest` against `LOCAL_TOOL_ENV`'s augmented
    `PATH`, which is how a tool in `~/.local/bin` is found on a host whose
    ambient `PATH` lacks that directory. The version call has to resolve
    against the same `PATH`: left on the ambient one it fails with 127 and
    prints nothing, so the floor is never enforced for the tool the probe
    just found, and an empty stream is all the check receives.

    Counted rather than matched per site, so a call site added without the
    environment fails here instead of being missed by a fixed list.
    """
    text = _makefile_text()
    total = text.count(command)
    guarded = text.count(f"$(LOCAL_TOOL_ENV) {command}")
    assert total > 0, (
        f"the Makefile must run {command!r}; the version floor is enforced "
        f"through it, and a check that was removed enforces nothing"
    )
    assert guarded == total, (
        f"{total - guarded} of {total} {command!r} invocations omit "
        f"LOCAL_TOOL_ENV, so they resolve against the ambient PATH while the "
        f"probe that found the tool used the augmented one; the call then "
        f"fails with 127 and prints nothing, and the floor is not enforced"
    )
