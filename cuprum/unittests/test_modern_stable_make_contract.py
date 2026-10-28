"""Hold the fragment-free modern stable Cargo route to its contract."""

from __future__ import annotations

import os
import re
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed Make argv.
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

from tests.helpers.docs import repo_root

MODERN_STABLE_TOOLCHAIN = "1.99.1"
MODERN_STABLE_TARGET_DIR = "rust/target/modern-stable"


def _dry_run(*goals: str, variables: dict[str, str] | None = None) -> str:
    """Return Make's expanded recipes for the selected route."""
    make = shutil.which("make")
    assert make is not None, "the routing contract requires GNU Make on PATH"
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make argv.
        [
            make,
            "--no-print-directory",
            "--dry-run",
            "CARGO=probe-cargo",
            *(f"{key}={value}" for key, value in (variables or {}).items()),
            *goals,
        ],
        capture_output=True,
        check=True,
        cwd=repo_root(),
        env={**os.environ, "MAKEFLAGS": ""},
        text=True,
    )
    return completed.stdout


def _modern_commands(output: str) -> list[str]:
    """Select expanded Cargo and Maturin commands from a Make dry run."""
    return [
        line
        for line in output.splitlines()
        if "probe-cargo" in line or "maturin develop" in line
    ]


def _makefile() -> str:
    """Return the Makefile text for static route assertions."""
    return (repo_root() / "Makefile").read_text(encoding="utf-8")


def test_modern_route_owns_its_toolchain_flags_and_target_directory() -> None:
    """Pin the modern route separately from the source MSRV and dev-fast."""
    makefile = _makefile()
    assert f"override MODERN_STABLE_TOOLCHAIN := {MODERN_STABLE_TOOLCHAIN}" in (
        makefile
    ), "the route must select its reviewed compiler explicitly"
    assert "override MODERN_STABLE_RUSTFLAGS := -C codegen-units=1" in makefile, (
        "modern rustflags may contain only the shared code-generation choice"
    )
    target_dir_assignment = (
        "override MODERN_STABLE_TARGET_DIR := "
        "$(CURDIR)/$(RUST_DIR)/target/modern-stable"
    )
    assert target_dir_assignment in makefile, (
        "the modern route needs its own worktree-local target directory"
    )
    output = "\n".join(_modern_commands(_dry_run("modern-stable-check")))
    for required in (
        "RUSTUP_TOOLCHAIN=1.99.1",
        "CARGO_BUILD_WARNINGS=deny",
        "CARGO_TARGET_DIR=",
        MODERN_STABLE_TARGET_DIR,
        'RUSTFLAGS="-C codegen-units=1"',
        "env -u RUSTC -u CARGO_ENCODED_RUSTFLAGS",
    ):
        assert required in output, f"modern Cargo commands must include {required!r}"
    assert "--config" not in output, "modern commands must not select a Cargo fragment"
    route_text = re.sub(r'PATH="[^"]*"', "PATH=<elided>", output)
    for forbidden in ("dev-fast/cargo", "nightly-2026-08-23", "mold", "Cranelift"):
        assert forbidden not in route_text, (
            f"unexpected option in modern route: {forbidden}"
        )


def test_modern_route_overrides_caller_warning_and_compiler_flags() -> None:
    """Prevent ambient warning policy and compiler flags entering the route."""
    output = "\n".join(
        _modern_commands(
            _dry_run(
                "modern-stable-check",
                variables={
                    "CARGO_BUILD_WARNINGS": "warn",
                    "RUSTFLAGS": "-D warnings",
                    "CARGO_ENCODED_RUSTFLAGS": "ambient",
                    "RUSTC": "ambient-rustc",
                    "MODERN_STABLE_CARGO_ENV": "CARGO_BUILD_WARNINGS=warn",
                    "MODERN_STABLE_CARGO": "ambient-cargo",
                    "MODERN_STABLE_RUSTFLAGS": "-D warnings",
                    "MODERN_STABLE_TOOLCHAIN_CHECK_COMMAND": "true",
                    "MODERN_STABLE_TEST_COMMAND": "true",
                },
            )
        )
    )
    assert "CARGO_BUILD_WARNINGS=deny" in output, (
        "the modern route must force Cargo warning denial"
    )
    assert "CARGO_BUILD_WARNINGS=warn" not in output, (
        "caller warning policy must not weaken the modern route"
    )
    assert 'RUSTFLAGS="-C codegen-units=1"' in output, (
        "ambient Rust flags must be replaced by the modern code-generation flag"
    )
    assert "RUSTFLAGS=-D warnings" not in output, (
        "caller Rust flags must not re-enter the modern command"
    )
    assert "ambient-rustc" not in output, "the route must clear a caller RUSTC"
    assert "CARGO_ENCODED_RUSTFLAGS=ambient" not in output, (
        "the route must clear encoded caller Rust flags"
    )

    make = shutil.which("make")
    assert make is not None, "the route contract requires GNU Make"
    inspection_target = (
        "modern-stable-override-contract:\n"
        "\t@printf '%s\\n' "
        "'$(origin MODERN_STABLE_TOOLCHAIN_CHECK_COMMAND)' "
        "'$(if $(findstring rustup component list,$(value "
        "MODERN_STABLE_TOOLCHAIN_CHECK_COMMAND)),preserved,missing)' "
        "'$(origin MODERN_STABLE_TEST_COMMAND)' "
        "'$(if $(findstring nextest run,$(value "
        "MODERN_STABLE_TEST_COMMAND)),preserved,missing)'\n"
    )
    inspection = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make argv.
        [
            make,
            "--no-print-directory",
            "MODERN_STABLE_TOOLCHAIN_CHECK_COMMAND=true",
            "MODERN_STABLE_TEST_COMMAND=true",
            "--eval",
            inspection_target,
            "modern-stable-override-contract",
        ],
        capture_output=True,
        check=False,
        cwd=repo_root(),
        env={**os.environ, "MAKEFLAGS": ""},
        text=True,
    )
    assert inspection.returncode == 0, inspection.stderr
    assert inspection.stdout.splitlines() == [
        "override",
        "preserved",
        "override",
        "preserved",
    ], "caller command variables must not replace the toolchain guard or test run"


def test_toolchain_guard_rejects_a_cargo_override_that_ignores_rustup(
    tmp_path: Path,
) -> None:
    """Reject a Cargo override without requiring an installed modern toolchain."""
    executable = shutil.which("make")
    assert executable is not None, "the toolchain guard requires GNU Make"
    rustup = tmp_path / "rustup"
    rustup.write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        "  'component list') printf 'clippy-x86_64-unknown-linux-gnu\\n' ;;\n"
        f"  'run {MODERN_STABLE_TOOLCHAIN}') printf "
        f"'rustc {MODERN_STABLE_TOOLCHAIN} (fixture)\\n"
        "binary: rustc\\n"
        f"release: {MODERN_STABLE_TOOLCHAIN}\\n"
        "LLVM version: fixture\\n' ;;\n"
        "  *) exit 64 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    rustup.chmod(0o755)
    cargo = tmp_path / "cargo-without-rustup-selection"
    cargo.write_text(
        "#!/bin/sh\nprintf 'cargo 1.85.0 (ignored toolchain)\\n'\n",
        encoding="utf-8",
    )
    cargo.chmod(0o755)
    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Make argv.
        [executable, f"CARGO={cargo}", "modern-stable-toolchain-check"],
        capture_output=True,
        check=False,
        cwd=repo_root(),
        env={
            **os.environ,
            "MAKEFLAGS": "",
            "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        text=True,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0, "the guard must reject an unselected Cargo"
    assert "does not honour rustup toolchain selection" in output, (
        "the toolchain guard must explain the rejected Cargo override"
    )


def test_modern_check_runs_all_required_gates_in_order() -> None:
    """Keep checks, Clippy, tests, docs, and doctests sequential and locked."""
    output = _dry_run("modern-stable-check")
    make = shutil.which("make")
    assert make is not None, "the route contract requires GNU Make"
    targets = (
        "modern-stable-cargo-check",
        "modern-stable-clippy",
        "modern-stable-test",
        "modern-stable-doc",
        "modern-stable-doctest",
    )
    recipe_lines = output.splitlines()
    positions = [recipe_lines.index(f"{make} {target}") for target in targets]
    assert positions == sorted(positions), "modern stable gates must run sequentially"
    commands = _modern_commands(output)
    cargo_commands = [line for line in commands if "probe-cargo" in line]
    assert cargo_commands, "the modern stable target must execute Cargo"
    build_commands = [
        line
        for line in cargo_commands
        if re.search(r"probe-cargo (?:check|clippy|nextest|test|doc)\b", line)
    ]
    assert build_commands, "the modern check must expose its Cargo build commands"
    assert all("--locked" in line for line in build_commands), (
        "every modern Cargo build invocation must preserve the checked-in lockfile"
    )
    assert all(
        "--cargo-verbose" in line if "nextest run" in line else "--verbose" in line
        for line in build_commands
    ), "modern Cargo gates must expose package freshness for warning controls"


def test_modern_clippy_moves_only_warning_fatality_to_cargo() -> None:
    """Use Cargo warning denial without dropping Clippy's lint pass."""
    output = _dry_run("modern-stable-clippy")
    clippy = next(line for line in output.splitlines() if "probe-cargo clippy" in line)
    assert "CARGO_BUILD_WARNINGS=deny" in clippy, (
        "modern Clippy must use Cargo warning denial"
    )
    assert "--workspace --all-targets --all-features" in clippy, (
        "modern Clippy must cover every workspace target and feature"
    )
    assert "-- -D warnings" not in clippy, (
        "modern Clippy must not key warnings in Rust compiler flags"
    )
    assert "--verbose" in clippy, "modern Clippy must expose freshness status"
    assert "-C codegen-units=1" in clippy, (
        "modern Clippy must retain the code-generation setting"
    )


def test_modern_documentation_keeps_stable_rustdoc_warning_flags() -> None:
    """Keep rustdoc and doctest warning enforcement outside the migration."""
    documentation = _dry_run("modern-stable-doc")
    doctests = _dry_run("modern-stable-doctest")
    assert 'RUSTDOCFLAGS="--cfg docsrs -D warnings"' in documentation, (
        "modern rustdoc must retain its warning-fatality flags"
    )
    assert 'RUSTDOCFLAGS="--cfg docsrs -D warnings"' in doctests, (
        "modern doctests must retain their stable-compatible warning flags"
    )
    for output in (documentation, doctests):
        assert "CARGO_BUILD_WARNINGS=deny" not in output, (
            "modern rustdoc and doctests must retain separate warning controls"
        )
        assert "--verbose" in output, "documentation gates must expose freshness"
        assert "-Zunstable-options" not in output, (
            "stable documentation must omit nightly-only flags"
        )
        assert "--display-doctest-warnings" not in output, (
            "stable doctests must omit the nightly warning display flag"
        )
        assert "--doctest-build-arg" not in output, (
            "stable doctests must omit nightly compiler arguments"
        )


def test_modern_test_threads_do_not_become_cargo_build_jobs() -> None:
    """Keep test concurrency separate from Cargo's build-job setting."""
    output = _dry_run("modern-stable-test", variables={"TEST_JOBS": "4"})
    test_commands = [
        line
        for line in output.splitlines()
        if "nextest run" in line or "probe-cargo test --locked" in line
    ]
    assert test_commands, "modern stable tests must select nextest or Cargo test"
    assert any(
        "--test-threads 4" in line or "--test-threads=4" in line
        for line in test_commands
    ), "TEST_JOBS must configure test threads"
    assert all(
        not re.search(r"(?:^|\s)--(?:build-)?jobs(?:=|\s)", line)
        for line in test_commands
    ), "TEST_JOBS must not cap Cargo's build parallelism"


def test_modern_develop_passes_locked_and_uses_the_modern_environment() -> None:
    """Send Maturin the same selected Cargo and warning policy as other gates."""
    output = _dry_run("modern-stable-develop")
    command = next(line for line in output.splitlines() if "maturin develop" in line)
    for required in (
        "RUSTUP_TOOLCHAIN=1.99.1",
        "CARGO_BUILD_WARNINGS=deny",
        'RUSTFLAGS="-C codegen-units=1"',
        "CARGO=probe-cargo",
        "maturin develop",
        "--verbose",
        "--manifest-path rust/cuprum-rust/Cargo.toml",
        "--locked",
    ):
        assert required in command, f"Maturin command must include {required!r}"
    output_order = (
        output.index("python -m ensurepip --upgrade"),
        output.index("maturin develop"),
    )
    assert output_order == tuple(sorted(output_order)), (
        "modern develop must prepare pip before Maturin installs the extension"
    )
    assert "--config" not in command, "modern Maturin must not select a fragment"
    assert "tools/dev-fast/cargo" not in command, (
        "modern Maturin must not use the dev-fast Cargo adapter"
    )


def test_modern_extension_and_aggregate_targets_preserve_order() -> None:
    """Rebuild the extension before requiring every extension-gated test."""
    extension = _dry_run("modern-stable-extension")
    ordered = (
        "make modern-stable-toolchain-check",
        "make modern-stable-develop",
        "make test-extension",
    )
    positions = [extension.index(target) for target in ordered]
    assert positions == sorted(positions), (
        "the extension target must check, develop, then run extension tests"
    )

    aggregate = _dry_run("modern-stable")
    check = aggregate.index("make modern-stable-check")
    extend = aggregate.index("make modern-stable-extension")
    assert check < extend, "modern-stable must finish checks before the extension suite"


def test_existing_routes_keep_their_warning_policy() -> None:
    """Retain warning-fatality flags on MSRV, dev-fast, rustdoc, and Whitaker."""
    makefile = _makefile()
    for required in (
        "RUST_FLAGS ?= -D warnings",
        "RUSTDOC_FLAGS ?= --cfg docsrs -D warnings",
        (
            "DOCTEST_RUSTDOC_FLAGS = $(RUSTDOC_FLAGS) "
            "$(if $(DEV_FAST_HOST_IS_LINUX),-Zunstable-options"
        ),
        "TEST_RUSTFLAGS ?= $(RUST_FLAGS) -C codegen-units=1",
        "WHITAKER_RUSTFLAGS ?= $(RUST_FLAGS) -C codegen-units=1",
        (
            "DEV_FAST_TEST_RUSTFLAGS = $(TEST_RUSTFLAGS) "
            "$(if $(DEV_FAST_HOST_IS_LINUX),-Clink-arg=-fuse-ld=mold)"
        ),
        "CLIPPY_FLAGS ?= $(CARGO_FLAGS) -- $(RUST_FLAGS)",
    ):
        assert required in makefile, f"the retained route policy omitted {required!r}"

    clippy_route = _dry_run("lint-clippy", variables={"DEV_FAST_HOST_IS_LINUX": ""})
    test_route = _dry_run("test-rust", variables={"DEV_FAST_HOST_IS_LINUX": ""})
    msrv_route = _dry_run("msrv-check")
    assert "clippy --all-targets --all-features -- -D warnings" in clippy_route, (
        "the retained Clippy route must keep warning fatality in compiler flags"
    )
    assert re.search(
        r'RUSTFLAGS="-D warnings -C codegen-units=1 ?"\s+probe-cargo test\b',
        test_route,
    ), "the retained test route must keep its established warning flags"
    assert "RUSTUP_TOOLCHAIN=1.85.0" in msrv_route, (
        "the MSRV route must remain pinned to Rust 1.85.0"
    )
    assert "DOCTEST_RUSTDOC_FLAGS" in makefile, (
        "nightly doctests must retain their separate rustdoc flags"
    )

    assert 'rustup run "1.85.0" rustc -vV' in msrv_route, (
        "the MSRV check must print the selected rustc version"
    )
    assert 'RUSTUP_TOOLCHAIN="1.85.0" probe-cargo -V' in msrv_route, (
        "the MSRV check must print the selected Cargo version"
    )
