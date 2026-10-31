"""Unit tests for the modern toolchain availability probe.

The probe decides whether the Cargo warning-policy contracts run or skip, so
its logic needs witnesses that do not depend on the machine having the modern
toolchain installed. These tests inject a locator and a probe, so they touch
neither `PATH` nor a real process.
"""

from __future__ import annotations

import subprocess  # ruff: ignore[suspicious-subprocess-import] - CompletedProcess stubs only.
import typing as typ

import pytest

from tests.helpers.cargo_warning_fixture import (
    ToolchainProbeError,
    toolchain_available,
)

if typ.TYPE_CHECKING:
    from tests.helpers import cargo_warning_fixture as fixture

TOOLCHAIN = "9.9.9"


def _process(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    """Return a completed process carrying the given output."""
    return subprocess.CompletedProcess([], returncode, stdout, "")


def _locate(name: str) -> str | None:
    """Resolve every executable a probe might ask for."""
    return f"/fixture/{name}"


def _probe(*, rustc: str, cargo: str, clippy: bool) -> fixture.Probe:
    """Build a probe that answers each toolchain query from fixed facts."""

    def probe(argv: list[str]) -> subprocess.CompletedProcess[str]:
        """Answer one toolchain query from the fixed facts."""
        if "run" in argv:
            return _process(f"rustc {rustc} (fixture)\n")
        if argv[-1] == "-V":
            return _process(f"cargo {cargo} (fixture)\n")
        components = "clippy-x86_64-unknown-linux-gnu\n"
        return _process(components if clippy else "rustfmt-x86_64\n")

    return probe


@pytest.mark.parametrize(
    ("rustc", "cargo", "clippy", "is_expected"),
    [
        (TOOLCHAIN, TOOLCHAIN, True, True),
        ("1.85.0", TOOLCHAIN, True, False),
        (TOOLCHAIN, "1.85.0", True, False),
        (TOOLCHAIN, TOOLCHAIN, False, False),
    ],
    ids=[
        "complete",
        "rustc outside the toolchain",
        "cargo outside the toolchain",
        "no clippy",
    ],
)
def test_each_requirement_decides_availability(
    rustc: str, cargo: str, clippy: bool, is_expected: bool
) -> None:
    """Require rustc, Cargo, and Clippy to agree on one toolchain."""
    is_available = toolchain_available(
        toolchain=TOOLCHAIN,
        probe=_probe(rustc=rustc, cargo=cargo, clippy=clippy),
        locate=_locate,
    )
    assert is_available is is_expected


def test_a_missing_executable_is_unavailable_without_probing() -> None:
    """Report absence without starting a probe for an executable not found."""

    def forbidden(argv: list[str]) -> subprocess.CompletedProcess[str]:
        """Fail the test if the query starts any probe at all."""
        message = f"no probe should run: {argv}"
        raise AssertionError(message)

    assert not toolchain_available(probe=forbidden, locate=lambda name: None)


def test_a_probe_that_gives_no_answer_is_unavailable() -> None:
    """Count a silent probe as unavailable so a loaded host skips, not errors."""
    assert not toolchain_available(probe=lambda argv: None, locate=_locate)


def test_the_query_probes_the_toolchain_it_is_given() -> None:
    """Carry the explicit toolchain into every probe the query issues."""
    seen: list[list[str]] = []

    def probe(argv: list[str]) -> subprocess.CompletedProcess[str]:
        """Record each argument vector and answer as the named toolchain."""
        seen.append(argv)
        if "run" in argv:
            return _process(f"rustc {TOOLCHAIN} (fixture)\n")
        if argv[-1] == "-V":
            return _process(f"cargo {TOOLCHAIN} (fixture)\n")
        return _process("clippy-x86_64-unknown-linux-gnu\n")

    assert toolchain_available(toolchain=TOOLCHAIN, probe=probe, locate=_locate)
    assert seen == [
        ["/fixture/rustup", "run", TOOLCHAIN, "rustc", "-V"],
        ["/fixture/cargo", f"+{TOOLCHAIN}", "-V"],
        [
            "/fixture/rustup",
            "component",
            "list",
            "--installed",
            "--toolchain",
            TOOLCHAIN,
        ],
    ], "every probe must select the toolchain the caller passed"


def test_a_probe_that_cannot_run_is_an_environment_fault() -> None:
    """Raise rather than misreport an unstartable probe as a missing toolchain."""
    with pytest.raises(ToolchainProbeError, match="toolchain probe could not start"):
        toolchain_available(locate=lambda name: "/nonexistent/cuprum-probe")
