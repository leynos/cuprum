"""Exercise the fail-closed Whitaker Makefile boundary."""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - controlled Make argv.
import sys
import typing as typ

import pytest

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    from collections import abc as cabc
    from pathlib import Path


_PACKAGES = ("cuprum-rust", "cuprum-streams", "cuprum-native-io")


def _make_executable() -> str:
    """Return the GNU Make executable required by the routing contract."""
    executable = shutil.which("make")
    assert executable is not None, "Whitaker Makefile tests require GNU Make"
    return executable


def _dry_run(
    *,
    variables: dict[str, str] | None = None,
    target: str = "lint-whitaker",
    makefiles: cabc.Sequence[Path] = (),
) -> str:
    """Return the evaluated Whitaker recipe with inert caller tools."""
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make argv.
        [
            _make_executable(),
            "--dry-run",
            *[argument for path in makefiles for argument in ("-f", str(path))],
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


def test_caller_cargo_flags_cannot_reach_whitaker() -> None:
    """A caller-supplied `CARGO_FLAGS` must not pass Whitaker's `--` separator.

    `CARGO_FLAGS` is caller-overridable, so forwarding it would let a caller
    append `--config ../tools/dev-fast/config.toml` and quietly acquire the
    development fragment on a route that must stay fragment-free.
    """
    command = _whitaker_command(
        _dry_run(
            variables={
                "CARGO_FLAGS": "--all-targets --config ../tools/dev-fast/config.toml"
            }
        )
    )

    assert "--config" not in command, "caller Cargo flags must not reach Whitaker"
    assert command.split(" -- ", 1)[1].split() == [
        *[argument for package in _PACKAGES for argument in ("--package", package)],
        "--all-targets",
        "--all-features",
        "--jobs",
        "1",
    ], "the audited flag list must be the whole of what Whitaker receives"


def test_caller_package_flags_cannot_displace_audited_packages() -> None:
    """The derived package flags must be `override`n, not just the package list.

    Marking only `WHITAKER_PACKAGES` leaves the flags built from it writable, so
    a caller can pass `WHITAKER_PACKAGE_FLAGS=--package evil` to displace every
    audited package while the list itself still looks intact.
    """
    command = _whitaker_command(
        _dry_run(variables={"WHITAKER_PACKAGE_FLAGS": "--package evil"})
    )

    assert "--package evil" not in command, (
        "caller-derived package flags must not displace audited packages"
    )
    assert all(f"--package {package}" in command for package in _PACKAGES), (
        "every audited package must survive a caller-supplied flag override"
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


def test_lint_target_runs_the_leaf_hierarchy_without_wait_markers() -> None:
    """The hosted Make route serializes leaves without GNU Make 4.4 syntax."""
    makefile = (repo_root() / "Makefile").read_text(encoding="utf-8")
    output = _dry_run(target="lint")

    # Strip comments first: the Makefile names `.WAIT` in prose to record why it
    # is deliberately unused, and a raw search would match that explanation.
    directives = "\n".join(
        line for line in makefile.splitlines() if not line.lstrip().startswith("#")
    )
    assert ".WAIT" not in directives, "the binding must support the hosted Make"
    assert output.index("python-lint") < output.index("probe-whitaker --all --"), (
        "Python lint must complete before the Whitaker leaf starts"
    )
    assert output.index("probe-whitaker --all --") < output.index(
        "yamllint --strict --config-file"
    ), "GitHub Actions lint must run after the Rust leaves"


@pytest.mark.parametrize("target", ["lint-clippy", "rust-lint", "lint"])
def test_included_override_fragments_apply_exactly_once(
    tmp_path: Path, target: str
) -> None:
    """A caller fragment reached through `include` must not be applied twice.

    GNU Make records `-f` inputs and files reached through `include` in the same
    `MAKEFILE_LIST`, with nothing to tell them apart. Forwarding that list to a
    sub-make therefore re-reads an included fragment once per recursion level,
    so a non-idempotent `CARGO_FLAGS += ...` compounds: `--fragment-marker`
    appeared 3 times at `lint` and twice at `rust-lint` before this regression
    test. Sequencing the leaves inside one Make process removes the sub-make
    and the second read with it.
    """
    fragment = tmp_path / "common.mk"
    fragment.write_text("CLIPPY_FLAGS += --fragment-marker\n", encoding="utf-8")
    override = tmp_path / "override.mk"
    override.write_text(f"include {fragment}\n", encoding="utf-8")

    # The repository Makefile must come first: a bare `-f override.mk` replaces
    # the implicit default rather than augmenting it, leaving `lint` undefined.
    output = _dry_run(target=target, makefiles=(repo_root() / "Makefile", override))

    assert output.count("--fragment-marker") == 1, (
        f"{target} must apply a caller's included fragment exactly once"
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
