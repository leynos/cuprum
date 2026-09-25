"""Contract for nextest's discovery of the repository's configuration.

`rust/.config/nextest.toml` declares the three tiers this branch adds, and
the contract tests that read it are satisfied by any file that parses:
they read the *file*, not the run. So a configuration nextest never loads
passes every one of them while declaring three tiers that bound nothing.
That is not hypothetical — nextest resolves `.config/nextest.toml` from the
Cargo workspace root and searches no parent directory, so the same file one
level up at the repository root would be read by nothing, and Cuprum keeps
no root `Cargo.toml` to make the repository root the workspace.

`test_timeout_ordering_contract.py` pins where the file sits relative to
the workspace manifest. This module covers the half that only the tool can
show: that nextest resolves *this* file from *this* directory. It asks
nextest to report the version requirement the configuration declares, which
is a value nextest reads from the resolved file and prints, so deleting the
file changes the answer.

The command is deliberately the cheapest one nextest has — it resolves the
workspace and reads the configuration without building a test binary or
running a test. The cost is a process, not a compilation, which is what
makes an end-to-end discovery check affordable inside the unit suite. Both
the resolved discovery and the control that proves it is a reading are
here: a document written into a workspace of its own shows what nextest
reports when the configuration is somewhere it does not look, so a run that
reported the hardcoded constant would fail rather than pass.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, reads the local configuration.
import typing as typ
from pathlib import (
    Path,  # ruff: ignore[typing-only-standard-library-import] - pytest resolves parameter annotations at runtime.
)

import pytest

from cuprum.unittests._timeout_lane_support import (
    CARGO_WORKSPACE_DIR,
    EXPECTED_NEXTEST_MIN_VERSION,
    NEXTEST_CONFIG,
)
from tests.helpers.docs import repo_root

#: The nextest subcommand that reports the requirement a configuration
#: declares. It resolves the workspace and reads `.config/nextest.toml`
#: without building a test binary, which is what makes this affordable in a
#: unit suite. `--color=never` is passed because the requirement is read back
#: out of this output, and nextest colours it whenever it believes the
#: destination is a terminal. Asking for plain text as an argument is
#: stronger than turning it off through the environment: this is the tool's
#: own flag for the choice, so the read never depends on how the process's
#: standard output was judged.
_DISCOVERY_COMMAND: typ.Final[tuple[str, ...]] = (
    "nextest",
    "show-config",
    "version",
    "--color=never",
)

#: A floor no released nextest satisfies, used by the control below.
#: Unreachable in the repository, where the obligation is the released
#: floor, so a run that read the constant rather than the file cannot
#: produce it.
_CONTROL_VERSION: typ.Final[str] = "9.9.9"

_REQUIREMENT = re.compile(r"^\s*-\s*required:\s*(\S+)\s*$", re.MULTILINE)

#: Make's own variables, dropped from the child environment so the nested
#: `make --dry-run` below is a plain invocation rather than one inheriting
#: the parent's jobserver and options.
_INHERITED_MAKE_ENV: typ.Final[frozenset[str]] = frozenset({
    "MAKEFLAGS",
    "MFLAGS",
    "CARGO_MAKEFLAGS",
})


def _nextest_available() -> bool:
    """Return whether `cargo-nextest` is on PATH.

    Returns
    -------
    bool
        Whether the tool the discovery claim is about can be run here.

    Read through `shutil.which` rather than by running it, so an absent
    tool skips these two tests instead of failing them. The claim under
    test belongs to nextest: a host without it cannot speak to it either
    way, and the configuration is still held by the file-reading contracts.
    """
    return shutil.which("cargo-nextest") is not None


def _discovery_env() -> dict[str, str]:
    """Return the environment the discovery command runs under.

    Returns
    -------
    dict[str, str]
        The parent environment, unchanged but for Make's inherited
        variables being dropped, so the `cargo` invocation below is not
        itself a nested one.

    The parent environment is deliberately otherwise carried through: the
    claim under test is about where nextest looks for its configuration,
    and a run with a stripped environment would be a different claim.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if key not in _INHERITED_MAKE_ENV
    }


def _reported_requirement(workspace: Path) -> tuple[int, str | None]:
    """Return nextest's exit status and the requirement it read.

    Parameters
    ----------
    workspace : Path
        The directory to resolve as the Cargo workspace.

    Returns
    -------
    tuple[int, str | None]
        The exit status, and the version the resolved configuration
        required — or ``None`` when the run reported no requirement.
    """
    cargo = shutil.which("cargo")
    assert cargo is not None, (
        "nextest is a Cargo subcommand; reading what a configuration "
        "declares needs `cargo` to resolve the workspace it sits in"
    )
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell needed.
        [cargo, *_DISCOVERY_COMMAND],
        capture_output=True,
        check=False,
        cwd=workspace,
        env=_discovery_env(),
        text=True,
    )
    match = _REQUIREMENT.search(completed.stdout)
    return completed.returncode, match.group(1) if match else None


def _workspace() -> Path:
    """Return the Cargo workspace directory, pinned to the config's parent."""
    return repo_root() / CARGO_WORKSPACE_DIR


@pytest.mark.skipif(
    not _nextest_available(), reason="no cargo-nextest on PATH to resolve the config"
)
def test_nextest_resolves_the_configured_floor_from_its_own_directory() -> None:
    """The tiers nextest enforces come from the file this branch installed.

    A refusal would be reported just as loudly by a lower floor as by the
    one the file declares, so the value is read back out of the report
    rather than the status alone. That is the property the file-reading
    contracts cannot show and nextest can.

    The floor is reported as a string in a field of its own, so any ANSI
    escape around it is found by the same pattern; `--color=never` in the
    command keeps that read to plain text.
    """
    status, required = _reported_requirement(_workspace())
    assert required is not None, (
        f"nextest reported no version requirement from {NEXTEST_CONFIG} in "
        f"{CARGO_WORKSPACE_DIR}/; the file was not resolved, so the tiers it "
        f"declares bound nothing while every contract that reads it passes"
    )
    assert required == EXPECTED_NEXTEST_MIN_VERSION, (
        f"nextest read a floor of {required!r} from {NEXTEST_CONFIG}, not the "
        f"{EXPECTED_NEXTEST_MIN_VERSION} the contract pins; a lower floor lets "
        f"nextest start on a release that drops the whole-run budget silently"
    )
    assert status == 0, (
        f"nextest refused to start on its own configuration with a "
        f"{EXPECTED_NEXTEST_MIN_VERSION} floor (exit {status}); the installed "
        f"release is older than the tier the Makefile installs it for"
    )


@pytest.mark.skipif(
    not _nextest_available(), reason="no cargo-nextest on PATH to resolve the config"
)
def test_a_configuration_nextest_does_not_search_is_reported_as_absent(
    tmp_path: Path,
) -> None:
    """A document nextest does not look for is not a document it read.

    The control for the test above, and the reason its answer is a reading
    rather than a constant. A workspace is built whose configuration
    declares a floor no released nextest satisfies: one that resolves and
    is read fails the version check with a distinguishable status, one that
    resolves to another floor reports that floor, and one that is never
    found reports no requirement at all. Any of the three is information;
    only the first matches the repository's own run, so a report of the
    pinned constant from a workspace that does not declare it is caught
    here rather than trusted above.
    """
    manifest = tmp_path / "Cargo.toml"
    manifest.write_text(
        '[package]\nname = "discovery-control"\nversion = "0.0.0"\nedition = "2021"\n',
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("", encoding="utf-8")
    config = tmp_path / NEXTEST_CONFIG.relative_to(CARGO_WORKSPACE_DIR)
    config.parent.mkdir(parents=True)
    config.write_text(f'nextest-version = "{_CONTROL_VERSION}"\n', encoding="utf-8")

    _, required = _reported_requirement(tmp_path)
    assert required != EXPECTED_NEXTEST_MIN_VERSION, (
        f"a workspace declaring a {_CONTROL_VERSION} floor reported the "
        f"repository's own {EXPECTED_NEXTEST_MIN_VERSION}; the reading above "
        f"is a constant, not the configuration nextest resolved"
    )
    assert required == _CONTROL_VERSION, (
        f"nextest read {required!r} from a workspace whose configuration "
        f"declares {_CONTROL_VERSION}; the reported requirement is not the "
        f"one nextest resolves, so the check above proves nothing"
    )
