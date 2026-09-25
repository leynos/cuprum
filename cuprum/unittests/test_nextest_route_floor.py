"""Contract for the version check the two Rust routes enforce.

`rust/.config/nextest.toml` declares `global-timeout`, and nextest warns
about a configuration key it does not recognize and carries on. On a
release that predates that key the whole-run budget is dropped silently
while the suite still passes, so both routes that can run nextest refuse a
release below `NEXTEST_MIN_VERSION` before it starts.

`test_nextest_version_floor.py` holds the awk program and the environment
each call site resolves against. This module covers what only the routes
themselves can show: that the refusal reaches `nextest run` from both
`test-rust` and `dev-test`, that it stops the route rather than merely
printing, and that a release at the floor is let through to run the suite.

The routes are driven rather than read, because a correct check sitting in
a branch that nothing reaches is a check that never fires. A stubbed
`cargo-nextest` supplies the version and a stubbed `cargo` records what the
route would have run, so each route is exercised to its verdict without a
toolchain and without building anything. The recorded argv is read as well
as the exit status: a route that reported the refusal and then ran the
suite anyway would drop the whole-run budget exactly as an unguarded one
does, and the status alone cannot tell those two apart.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, drives the local Makefile.
import typing as typ
from pathlib import (
    Path,  # ruff: ignore[typing-only-standard-library-import] - pytest resolves parameter annotations at runtime.
)

import pytest

from cuprum.unittests._timeout_lane_support import EXPECTED_NEXTEST_MIN_VERSION
from tests.helpers.docs import repo_root

#: The routes that can run nextest, and so carry the floor. Both are here
#: because the check is written twice -- once in `test-rust`'s recipe and
#: once in `DEV_FAST_TEST_COMMAND` -- and a test that drove one would leave
#: the other free to lose its gate.
_DRIVEN_ROUTES: typ.Final[tuple[str, ...]] = ("test-rust", "dev-test")

#: The version lines the stubbed `cargo-nextest` reports. The supported one
#: is built from the floor rather than hardcoded, so the boundary case this
#: module exists to check cannot drift away from the value it is checking:
#: at the floor is the one release the check must not refuse, and one
#: below it is the one it must.
_SUPPORTED_VERSION: typ.Final[str] = (
    f"cargo-nextest {EXPECTED_NEXTEST_MIN_VERSION} (65e806bd5 2026-04-14)"
)
_BELOW_FLOOR_VERSION: typ.Final[str] = "cargo-nextest 0.9.54 (deadbeef 2026-01-01)"

#: Make's own variables, dropped from the child environment. `MAKEFLAGS`
#: carries the parent's jobserver and options into the child, and a nested
#: `make` that inherits them is not the plain invocation this module is
#: driving; the three spelling variants are how different Make versions and
#: the automake wrapper name them.
_INHERITED_MAKE_ENV: typ.Final[frozenset[str]] = frozenset({
    "MAKEFLAGS",
    "MFLAGS",
    "CARGO_MAKEFLAGS",
})


def _write_stubs(directory: Path, version: str | None) -> Path:
    """Write the stub tools into `directory` and return the cargo log path.

    Parameters
    ----------
    directory : Path
        The directory the stubs are written into.
    version : str | None
        The line the stubbed `cargo-nextest` prints, or ``None`` to leave
        the tool out altogether so the route takes its `cargo test`
        fallback.

    Returns
    -------
    Path
        The file the stubbed `cargo` appends its argv to, one line per
        call.

    The stubs are what lets a route reach its verdict without a toolchain
    and without building: `cargo-nextest` answers the version probe, and
    `cargo` records the command the route went on to run instead of running
    it. That record is how the caller distinguishes `nextest run` from
    `test`, which is the difference between the gate opening and closing.
    """
    log = directory / "cargo.log"
    log.write_text("", encoding="utf-8")
    if version is not None:
        nextest = directory / "cargo-nextest"
        nextest.write_text(f'#!/bin/sh\necho "{version}"\n', encoding="utf-8")
        nextest.chmod(0o755)
    cargo = directory / "cargo"
    cargo.write_text(
        f'#!/bin/sh\necho "CARGO $*" >> "{log}"\n',
        encoding="utf-8",
    )
    cargo.chmod(0o755)
    return log


def _route_env() -> dict[str, str]:
    """Return the environment one route is driven under.

    Returns
    -------
    dict[str, str]
        The parent environment with Make's inherited variables removed.

    The selections themselves are passed as command-line variables rather
    than through this mapping, and that is load-bearing: `LOCAL_TOOL_PATH`
    and `DEV_FAST_CHECK_COMMAND` are both assigned with `=` in the
    Makefile, which overrides an inherited environment value. A stub
    directory exported through the environment is therefore ignored, and
    the route resolves against the host's real tools — a test that would
    pass by driving the wrong `cargo-nextest` while appearing to control
    it. Command-line variables outrank the makefile's own assignment, which
    is what makes the substitution take.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if key not in _INHERITED_MAKE_ENV
    }


def _drive(
    target: str, stub_directory: Path, version: str | None, *, with_host_path: bool
) -> tuple[int, str]:
    """Drive one Makefile route with stubbed tools.

    Parameters
    ----------
    target : str
        The Makefile target to drive.
    stub_directory : Path
        The directory the stubs are written into.
    version : str | None
        The version the stubbed `cargo-nextest` reports, or ``None`` to
        omit the tool so the route takes its `cargo test` fallback.
    with_host_path : bool
        Whether the host's own `PATH` follows the stub directory. It must,
        for the routes that resolve a stubbed tool: the route reaches
        `cargo` through that same `PATH`, so a bare stub directory would
        leave the route with no Cargo at all and the test would be measuring
        that rather than the gate. It must not, when the stub is a tool the
        host also has: a fallback test that kept the host's `PATH` would
        find the host's real `cargo-nextest` past the stub directory and
        exercise the opposite branch.

    Returns
    -------
    tuple[int, str]
        The route's exit status and the commands its cargo received.
    """
    log = _write_stubs(stub_directory, version)
    make = shutil.which("make")
    assert make is not None, (
        "the two routes this module drives are Makefile targets; without "
        "`make` neither the gate nor its absence can be shown"
    )
    # `LOCAL_TOOL_PATH` is the variable both routes build their `PATH`
    # from, so putting the stubs at its front is how the probe and the
    # version call resolve to them -- the same mechanism, and the same
    # variable, that finds a real tool in `~/.local/bin`. `CARGO` is
    # pointed at the stub as well because the doctest line at the foot of
    # `test-rust` runs outside `LOCAL_TOOL_ENV` and would otherwise reach
    # the host's real compiler. `DEV_FAST_CHECK_COMMAND` is neutralized
    # because the accelerated route otherwise insists on a Cranelift
    # toolchain and a checksum-verified `mold`; those are dev-fast
    # prerequisites rather than anything this module is about, and the stub
    # cargo means no compiler is invoked either way.
    path = str(stub_directory)
    if with_host_path:
        path = f"{path}{os.pathsep}{os.environ.get('PATH', '')}"
    selections = (
        f"LOCAL_TOOL_PATH={path}",
        f"CARGO={stub_directory / 'cargo'}",
        "DEV_FAST_CHECK_COMMAND=true",
    )
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell needed.
        [make, *selections, target],
        capture_output=True,
        check=False,
        cwd=repo_root(),
        env=_route_env(),
        text=True,
    )
    return completed.returncode, log.read_text(encoding="utf-8")


@pytest.mark.parametrize("target", _DRIVEN_ROUTES)
def test_a_release_below_the_floor_stops_the_route(target: str, tmp_path: Path) -> None:
    """Both routes refuse a release under the floor before running it.

    The recorded argv is read as well as the status, because a route that
    printed the refusal and then ran the suite anyway would drop the
    whole-run budget exactly as the unguarded one does. Only the exit
    status would still look like a refusal, so a check that had lost its
    `exit 1` would pass a status-only test while enforcing nothing.
    """
    status, commands = _drive(
        target, tmp_path, _BELOW_FLOOR_VERSION, with_host_path=True
    )
    assert status != 0, (
        f"`{target}` ran a cargo-nextest below the "
        f"{EXPECTED_NEXTEST_MIN_VERSION} floor to a successful exit; the "
        f"release that drops the whole-run budget silently is the one the "
        f"floor exists to refuse"
    )
    assert "nextest run" not in commands, (
        f"`{target}` reported the floor and then ran nextest anyway. The "
        f"refusal has to stop the route, not print: nextest warns about the "
        f"`global-timeout` key it does not know and runs on without a "
        f"whole-run budget, which is the failure the floor prevents. Its "
        f"cargo received: {commands.strip()!r}"
    )


@pytest.mark.parametrize("target", _DRIVEN_ROUTES)
def test_a_release_at_the_floor_reaches_the_suite(target: str, tmp_path: Path) -> None:
    """A release exactly at the floor is let through to run the suite.

    At the floor is the boundary, and it is the case a check written with a
    strict comparison would wrongly refuse: the floor is a minimum, not an
    excluded value. A refusal here blocks every supported run, which is the
    other way a version check fails its purpose.
    """
    status, commands = _drive(target, tmp_path, _SUPPORTED_VERSION, with_host_path=True)
    assert status == 0, (
        f"`{target}` refused a cargo-nextest reporting "
        f"{EXPECTED_NEXTEST_MIN_VERSION}, which is the floor itself; a "
        f"minimum that excludes its own value blocks every supported run"
    )
    assert "nextest run" in commands, (
        f"`{target}` accepted the release and never ran the suite. Its "
        f"cargo received: {commands.strip()!r}"
    )


def test_a_host_without_the_tool_falls_back_to_cargo_test(tmp_path: Path) -> None:
    """A host with no nextest keeps the `cargo test` fallback.

    The floor is a gate on nextest, not a requirement to have it. The
    fallback predates this branch and is what a developer without the tool
    relies on, so a version check that refused an absent tool would turn an
    optional accelerator into a dependency of the default route.
    """
    status, commands = _drive("test-rust", tmp_path, None, with_host_path=False)
    assert status == 0, (
        "`test-rust` failed on a host with no cargo-nextest on the augmented "
        "PATH; the floor is a gate on the tool, not a requirement to have it"
    )
    assert "nextest" not in commands, (
        f"`test-rust` fell through to a nextest call with no cargo-nextest "
        f"to call. Its cargo received: {commands.strip()!r}"
    )
    assert " test" in commands, (
        f"`test-rust` did not reach the `cargo test` fallback. Its cargo "
        f"received: {commands.strip()!r}"
    )
