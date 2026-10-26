"""Contracts for the extension-only Rust/Python boundary parity target."""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed Make argv.

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


def test_boundary_target_selects_only_ignored_parity_cases() -> None:
    """The dedicated command must be non-vacuous and use the project Python."""
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
    assert "boundary parity target selected zero ignored tests" in output, (
        "a vacuous ignored-test selection must fail acceptance"
    )


def test_ordinary_test_route_keeps_boundary_parity_extension_free() -> None:
    """The new child bridge must not move into ordinary Cargo or pytest tests."""
    output = _make_dry_run("test")

    assert "consume_boundary_parity" not in output, (
        "ordinary `make test` must not invoke the interpreter-dependent target"
    )


def test_non_linux_boundary_route_uses_the_project_stable_cargo() -> None:
    """Simulate Windows/macOS selection and retain the plain pinned Cargo route."""
    output = _make_dry_run("DEV_FAST_HOST_IS_LINUX=", "test-boundary-parity")
    command = next(
        line
        for line in output.splitlines()
        if "probe-cargo" in line and "test -p cuprum-rust" in line
    )

    assert "nightly-2026-08-23" not in command
    assert "--config" not in command
