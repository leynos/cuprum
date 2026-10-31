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
    ToolchainProbeTimeoutError,
    require_toolchain,
    toolchain_available,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

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

    def probe(
        argv: list[str], _env: cabc.Mapping[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        """Answer one toolchain query from the fixed facts."""
        if "run" in argv:
            return _process(f"rustc {rustc} (fixture)\n")
        if argv[-1] == "-V":
            return _process(f"cargo {cargo} (fixture)\n")
        components = "clippy-x86_64-unknown-linux-gnu\n"
        return _process(components if clippy else "rustfmt-x86_64\n")

    return probe


def _timed_out_probe(
    argv: list[str], _env: cabc.Mapping[str, str] | None
) -> subprocess.CompletedProcess[str]:
    """Raise the timeout the real probe raises, without starting a process."""
    message = f"probe timed out: {argv}"
    raise ToolchainProbeTimeoutError(message)


def _unstartable_probe(
    argv: list[str], _env: cabc.Mapping[str, str] | None
) -> subprocess.CompletedProcess[str]:
    """Raise the start failure the real probe raises, without a process."""
    message = f"toolchain probe could not start: {argv}"
    raise ToolchainProbeError(message)


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
    assert is_available is is_expected, (
        f"availability for rustc={rustc}, cargo={cargo}, clippy={clippy}"
    )


def test_a_missing_executable_is_unavailable_without_probing() -> None:
    """Report absence without starting a probe for an executable not found."""

    def forbidden(
        argv: list[str], env: cabc.Mapping[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        """Fail the test if the query starts any probe at all."""
        message = f"no probe should run: {argv} with {env}"
        raise AssertionError(message)

    is_available = toolchain_available(probe=forbidden, locate=lambda name: None)
    assert not is_available, "an absent executable must report unavailability"


def test_a_probe_that_fails_on_its_own_terms_is_unavailable() -> None:
    """Read a non-zero probe answer as an absent toolchain, not a fault."""
    is_available = toolchain_available(
        probe=lambda argv, env: _process("", returncode=1), locate=_locate
    )
    assert not is_available, "a probe that failed its query must be unavailable"


def test_a_probe_that_outlives_its_bound_is_a_host_fault() -> None:
    """Propagate a scheduling failure instead of hiding it behind `False`."""
    with pytest.raises(ToolchainProbeTimeoutError, match="probe timed out"):
        toolchain_available(probe=_timed_out_probe, locate=_locate)


def test_the_query_selects_the_toolchain_it_is_given() -> None:
    """Select Cargo through the environment and rustup through its arguments."""
    seen: list[tuple[list[str], cabc.Mapping[str, str] | None]] = []

    def probe(
        argv: list[str], env: cabc.Mapping[str, str] | None
    ) -> subprocess.CompletedProcess[str]:
        """Record each query and its environment, and answer as available."""
        seen.append((argv, env))
        if "run" in argv:
            return _process(f"rustc {TOOLCHAIN} (fixture)\n")
        if argv[-1] == "-V":
            return _process(f"cargo {TOOLCHAIN} (fixture)\n")
        return _process("clippy-x86_64-unknown-linux-gnu\n")

    is_available = toolchain_available(toolchain=TOOLCHAIN, probe=probe, locate=_locate)
    assert is_available, "a complete toolchain must report availability"
    assert [argv for argv, _env in seen] == [
        ["/fixture/rustup", "run", TOOLCHAIN, "rustc", "-V"],
        ["/fixture/cargo", "-V"],
        [
            "/fixture/rustup",
            "component",
            "list",
            "--installed",
            "--toolchain",
            TOOLCHAIN,
        ],
    ], "each probe must address the toolchain the caller passed"
    assert [env for argv, env in seen if argv[0] == "/fixture/cargo"] == [
        {"RUSTUP_TOOLCHAIN": TOOLCHAIN}
    ], "Cargo must be selected through RUSTUP_TOOLCHAIN, not a +toolchain argument"
    assert all(env is None for argv, env in seen if argv[0] == "/fixture/rustup"), (
        "the rustup probes already name the toolchain, so they inherit the environment"
    )


def test_a_probe_that_cannot_run_is_an_environment_fault() -> None:
    """Raise rather than misreport an unstartable probe as a missing toolchain."""
    with pytest.raises(ToolchainProbeError, match="toolchain probe could not start"):
        toolchain_available(probe=_unstartable_probe, locate=_locate)


def test_a_timed_out_probe_skips_a_local_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skip a loaded local host instead of failing its contract run."""
    monkeypatch.delenv("CUPRUM_REQUIRE_MODERN_STABLE", raising=False)
    with pytest.raises(pytest.skip.Exception, match="probe timed out"):
        require_toolchain(probe=_timed_out_probe, locate=_locate)


def test_a_timed_out_probe_fails_a_required_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail the required CI fixture run when the toolchain cannot be probed."""
    monkeypatch.setenv("CUPRUM_REQUIRE_MODERN_STABLE", "1")
    with pytest.raises(pytest.fail.Exception, match="probe timed out"):
        require_toolchain(probe=_timed_out_probe, locate=_locate)
