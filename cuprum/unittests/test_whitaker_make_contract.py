"""Exercise the fail-closed Whitaker Makefile boundary."""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - controlled Make argv.
import sys
import typing as typ

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    from pathlib import Path


_PACKAGES = ("cuprum-rust", "cuprum-streams", "cuprum-native-io")


def _make_executable() -> str:
    """Return the GNU Make executable required by the routing contract."""
    executable = shutil.which("make")
    assert executable is not None, "Whitaker Makefile tests require GNU Make"
    return executable


def _dry_run(
    *, variables: dict[str, str] | None = None, target: str = "lint-whitaker"
) -> str:
    """Return the evaluated Whitaker recipe with inert caller tools."""
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make argv.
        [
            _make_executable(),
            "--dry-run",
            "CARGO=probe-cargo",
            "WHITAKER=probe-whitaker",
            *(f"{key}={value}" for key, value in (variables or {}).items()),
            target,
        ],
        capture_output=True,
        check=True,
        cwd=repo_root(),
        env={**os.environ, "MAKEFLAGS": ""},
        text=True,
    )
    return completed.stdout


def _whitaker_command(output: str) -> str:
    """Extract the one binding invocation from Make's evaluated recipe."""
    return next(
        line for line in output.splitlines() if "probe-whitaker --all --" in line
    )


def test_whitaker_target_covers_the_exact_workspace_packages() -> None:
    """Package selection reaches Cargo after the wrapper separator."""
    command = _whitaker_command(_dry_run())
    wrapper, separator, cargo_arguments = command.partition(" -- ")

    assert separator == " -- ", "Whitaker must receive Cargo flags after `--`"
    assert wrapper.endswith("probe-whitaker --all"), (
        "Whitaker must load the full lint suite before forwarding Cargo flags"
    )
    assert cargo_arguments.split() == [
        *[argument for package in _PACKAGES for argument in ("--package", package)],
        "--all-targets",
        "--all-features",
        "--jobs",
        "1",
    ], "the binding must enumerate every Rust workspace package exactly once"


def test_whitaker_package_scope_cannot_be_caller_overridden() -> None:
    """An arbitrary Make variable cannot remove an audited workspace package."""
    command = _whitaker_command(_dry_run(variables={"WHITAKER_PACKAGES": "other"}))

    assert "--package other" not in command, "caller scope must not reach Whitaker"
    assert all(f"--package {package}" in command for package in _PACKAGES), (
        "every audited package must remain in the binding"
    )


def test_whitaker_target_is_direct_and_fragment_free() -> None:
    """The binding cannot be skipped, masked, or routed through dev-fast."""
    command = _whitaker_command(_dry_run())

    assert "command -v" not in command, "the binding must not have an existence guard"
    assert "||" not in command, "the binding must not ignore a wrapper failure"
    assert "|" not in command, "the binding must not mask a wrapper failure"
    assert "--config" not in command, "Whitaker must not select a Cargo fragment"
    assert "nightly-2026-08-23" not in command, (
        "Whitaker must retain its independently pinned verifier toolchain"
    )


def test_rust_lint_runs_clippy_whitaker_and_spelling_in_order() -> None:
    """The aggregate Rust gate preserves the intended sequential leaf order."""
    output = _dry_run(target="rust-lint")
    clippy = output.index("probe-cargo --config ../tools/dev-fast/config.toml clippy")
    whitaker = output.index("probe-whitaker --all --")
    spelling = output.index("typos-config-builder gate --repository . --scope all")

    assert clippy < whitaker < spelling, (
        "rust-lint must finish Clippy before Whitaker and spelling"
    )


def test_whitaker_failure_propagates_through_make(tmp_path: Path) -> None:
    """A failing suite executable must fail the leaf target unchanged by a guard."""
    failing_whitaker = tmp_path / "failing-whitaker"
    failing_whitaker.write_text(
        f"#!{sys.executable}\nimport sys\nsys.exit(73)\n", encoding="utf-8"
    )
    failing_whitaker.chmod(0o755)

    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make argv.
        [_make_executable(), "lint-whitaker", f"WHITAKER={failing_whitaker}"],
        capture_output=True,
        check=False,
        cwd=repo_root(),
        env={**os.environ, "MAKEFLAGS": ""},
        text=True,
    )

    assert completed.returncode != 0, "a failing Whitaker executable must fail Make"
    assert "Error 73" in completed.stderr, (
        "Make must report the wrapper's original non-zero status"
    )
