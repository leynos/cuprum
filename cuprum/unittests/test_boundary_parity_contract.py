"""Contracts for the extension-only Rust/Python boundary parity target."""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed Make argv.

import pytest

from tests.helpers.docs import repo_root


def _make_dry_run(*arguments: str) -> str:
    """Return expanded Make recipes without running their commands."""
    make = shutil.which("make")
    assert make is not None, "the Makefile contract requires GNU Make"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"MAKEFLAGS", "MAKELEVEL", "MFLAGS"}
    }
    environment["MAKEFLAGS"] = ""
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make argv.
        [make, "--dry-run", "CARGO=probe-cargo", *arguments],
        capture_output=True,
        check=True,
        cwd=repo_root(),
        env=environment,
        text=True,
    )
    return completed.stdout


def test_extension_route_runs_boundary_parity_once_after_guarded_pytest() -> None:
    """Make the existing extension lane own one sequential parity run."""
    output = _make_dry_run("DEV_FAST_HOST_IS_LINUX=yes", "test-extension")
    lines = output.splitlines()
    guarded_pytest_index = next(
        index
        for index, line in enumerate(lines)
        if "CUPRUM_REQUIRE_RUST_EXTENSION=1" in line
    )
    boundary_make = "make --no-print-directory test-boundary-parity"
    boundary_index = next(
        index for index, line in enumerate(lines) if boundary_make in line
    )

    assert boundary_index > guarded_pytest_index, (
        "the parity process must run after extension-gated pytest completes"
    )
    assert output.count(boundary_make) == 1, (
        "the extension lane must invoke the boundary target exactly once"
    )
    assert (
        "tests/helpers/test_consume_boundary_oracle.py" in lines[guarded_pytest_index]
    ), "the extension lane must exercise the Python oracle helper"


def test_boundary_target_selects_all_ignored_parity_cases() -> None:
    """The command pins every witness and uses the project Python."""
    output = _make_dry_run("DEV_FAST_HOST_IS_LINUX=yes", "test-boundary-parity")
    cargo_lines = [
        line
        for line in output.splitlines()
        if "probe-cargo" in line and "test -p cuprum-rust" in line
    ]

    assert len(cargo_lines) == 1, (
        "the target must issue one Cargo integration-test command"
    )
    command = cargo_lines[0]
    assert "--test consume_boundary_parity" in command
    assert "-- --ignored --test-threads=1" in command
    assert "--workspace" not in command
    assert "--all-targets" not in command, (
        "the target must select only the boundary integration-test binary"
    )
    assert "uv run --no-sync python -c 'import sys; print(sys.executable)'" in output, (
        "the extension child must use the interpreter from the project environment"
    )
    assert "CUPRUM_BOUNDARY_PYTHON=" in output
    assert "boundary parity target did not select all 30 ignored tests" in output, (
        "a partial ignored-test selection must fail acceptance"
    )


def test_ordinary_test_route_keeps_boundary_parity_extension_free() -> None:
    """The new child bridge must not move into ordinary Cargo or pytest tests."""
    output = _make_dry_run("test")

    assert "consume_boundary_parity" not in output, (
        "ordinary `make test` must not invoke the interpreter-dependent target"
    )


@pytest.mark.parametrize(
    "host_linux",
    ["yes", ""],
    ids=("linux", "windows-or-macos"),
)
def test_boundary_route_uses_the_plain_cargo_on_every_host(host_linux: str) -> None:
    """The parity target must never select the nightly dev-fast route.

    ``test-extension`` is a prerequisite of the modern-stable lane, which the
    repository's own contracts forbid from provisioning dev-fast. Taking that
    route here made the modern-stable job demand a Cranelift component it never
    installs, so both simulated hosts are pinned to the plain Cargo route.
    """
    output = _make_dry_run(
        f"DEV_FAST_HOST_IS_LINUX={host_linux}", "test-boundary-parity"
    )
    command = next(
        line
        for line in output.splitlines()
        if "probe-cargo" in line and "test -p cuprum-rust" in line
    )

    assert "nightly-2026-08-23" not in command
    assert "--config" not in command
    assert "-Zthreads" not in command, (
        "the parity target must not inherit nightly-only dev-fast RUSTFLAGS"
    )
