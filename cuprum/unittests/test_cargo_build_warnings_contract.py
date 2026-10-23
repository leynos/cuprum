"""Prove Cargo warning policy changes preserve build freshness."""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed Cargo argv.
import sys
from pathlib import Path

import pytest

from tests.helpers.docs import repo_root

MODERN_STABLE_TOOLCHAIN = "1.99.1"
TARGET_DIR = repo_root() / "rust/target/cargo-build-warnings-contract"
RUSTDOC_PROBE_REPORT = TARGET_DIR / "rustdoc-warning-probe.txt"
BASE_RUSTFLAGS = "-C codegen-units=1"
CARGO_COMMAND_TIMEOUT_SECONDS = 7200
WARNING_SOURCE = "fn deliberately_unused() {}\npub fn ready() {}\n"
FIXED_SOURCE = "pub fn ready() {}\n"
CLIPPY_SOURCE = (
    "#![warn(clippy::needless_return)]\npub fn ready() -> u8 { return 1; }\n"
)
RUSTDOC_SOURCE = (
    "#![warn(rustdoc::broken_intra_doc_links)]\n"
    "/// Refers to [a missing item](crate::missing_item).\n"
    "pub struct DocumentationProbe;\n"
)


def _toolchain_available() -> bool:
    """Check that the selected compiler, Cargo, and Clippy are installed."""
    rustup = shutil.which("rustup")
    cargo = shutil.which("cargo")
    if rustup is None or cargo is None:
        return False
    try:
        rustc = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed toolchain probe.
            [rustup, "run", MODERN_STABLE_TOOLCHAIN, "rustc", "-V"],
            capture_output=True,
            check=False,
            timeout=10,
            text=True,
        )
        cargo_version = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed toolchain probe.
            [cargo, f"+{MODERN_STABLE_TOOLCHAIN}", "-V"],
            capture_output=True,
            check=False,
            timeout=10,
            text=True,
        )
        components = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed toolchain probe.
            [
                rustup,
                "component",
                "list",
                "--installed",
                "--toolchain",
                MODERN_STABLE_TOOLCHAIN,
            ],
            capture_output=True,
            check=False,
            timeout=10,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return (
        rustc.returncode == 0
        and rustc.stdout.startswith(f"rustc {MODERN_STABLE_TOOLCHAIN} ")
        and cargo_version.returncode == 0
        and cargo_version.stdout.startswith(f"cargo {MODERN_STABLE_TOOLCHAIN} ")
        and components.returncode == 0
        and any(line.startswith("clippy") for line in components.stdout.splitlines())
    )


def _require_toolchain() -> str:
    """Return Cargo or skip locally, while making CI absence a failure."""
    cargo = shutil.which("cargo")
    if cargo is None or not _toolchain_available():
        message = (
            f"Rust and Cargo {MODERN_STABLE_TOOLCHAIN} with Clippy are unavailable"
        )
        if os.environ.get("CUPRUM_REQUIRE_MODERN_STABLE") == "1":
            pytest.fail(message)
        pytest.skip(message)
    assert cargo is not None
    return cargo


def _host_target() -> str:
    """Return the exact host triple reported by the selected rustc."""
    rustup = shutil.which("rustup")
    assert rustup is not None, "rustup must be available with the pinned toolchain"
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed toolchain probe.
        [rustup, "run", MODERN_STABLE_TOOLCHAIN, "rustc", "-vV"],
        capture_output=True,
        check=True,
        timeout=10,
        text=True,
    )
    target = next(
        (
            line.removeprefix("host: ")
            for line in result.stdout.splitlines()
            if line.startswith("host: ")
        ),
        None,
    )
    assert target is not None, "rustc -vV must report its host target"
    return target


def _crate(tmp_path: Path, name: str, source: str) -> Path:
    """Create one dependency-free library with a fixed lockfile."""
    crate = tmp_path / name
    source_path = crate / "src/lib.rs"
    source_path.parent.mkdir(parents=True)
    (crate / "Cargo.toml").write_text(
        f'[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2024"\n',
        encoding="utf-8",
    )
    (crate / "Cargo.lock").write_text(
        f'version = 4\n\n[[package]]\nname = "{name}"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    source_path.write_text(source, encoding="utf-8")
    return crate


def _target_dir(crate: Path) -> Path:
    """Keep each pytest run isolated while sharing its policy-toggle cache."""
    return TARGET_DIR / crate.parent.name


def _run_cargo(
    cargo: str,
    target: str,
    crate: Path,
    *arguments: str,
    warnings: str = "deny",
    rustflags: str = BASE_RUSTFLAGS,
) -> subprocess.CompletedProcess[str]:
    """Run one locked command through the selected stable toolchain."""
    environment = os.environ.copy()
    environment.pop("RUSTC", None)
    environment.pop("CARGO_ENCODED_RUSTFLAGS", None)
    environment.update({
        "CARGO_BUILD_WARNINGS": warnings,
        "CARGO_TERM_COLOR": "never",
        "CARGO_TARGET_DIR": str(_target_dir(crate)),
        "RUSTFLAGS": rustflags,
        "RUSTUP_TOOLCHAIN": MODERN_STABLE_TOOLCHAIN,
    })
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Cargo argv.
        [
            cargo,
            *arguments,
            "--locked",
            "--offline",
            "--verbose",
            "--target",
            target,
            "--profile",
            "dev",
        ],
        capture_output=True,
        check=False,
        cwd=crate,
        env=environment,
        timeout=CARGO_COMMAND_TIMEOUT_SECONDS,
        text=True,
    )


def _diagnostics(result: subprocess.CompletedProcess[str]) -> str:
    """Return Cargo's combined output for a focused assertion message."""
    return result.stdout + result.stderr


def _assert_fails_on_warning(
    result: subprocess.CompletedProcess[str], package: str, lint: str
) -> str:
    """Require warning denial to be the failure and return Cargo output."""
    output = _diagnostics(result)
    assert result.returncode != 0, f"Cargo accepted {lint} under deny:\n{output}"
    assert lint in output, f"Cargo failed without reporting {lint}:\n{output}"
    assert (
        "error: warnings are denied by `build.warnings` configuration" in output
        or "error: could not compile" in output
    ), f"Cargo did not make {lint} fatal:\n{output}"
    assert package in output, f"Cargo did not identify the local package:\n{output}"
    return output


def _assert_compiles_warning(cargo: str, target: str, crate: Path) -> None:
    """Require a warning-level build to compile and report the fixture lint."""
    result = _run_cargo(cargo, target, crate, "build", warnings="warn")
    output = _diagnostics(result)
    assert result.returncode == 0, output
    assert "Compiling warning-policy-fixture" in output, output
    assert "deliberately_unused" in output, output


def _assert_denial_reuses_artifact(cargo: str, target: str, crate: Path) -> None:
    """Require a fresh Cargo replay to deny the cached fixture warning."""
    result = _run_cargo(cargo, target, crate, "build", warnings="deny")
    output = _assert_fails_on_warning(
        result, "warning-policy-fixture", "deliberately_unused"
    )
    assert "Fresh warning-policy-fixture" in output, output
    assert "Compiling warning-policy-fixture" not in output, (
        "the policy-only warn-to-deny change must reuse the compiled crate"
    )


def _assert_warning_recovery_reuses_artifact(
    cargo: str, target: str, crate: Path
) -> None:
    """Require warning policy recovery to replay the cached build cleanly."""
    result = _run_cargo(cargo, target, crate, "build", warnings="warn")
    output = _diagnostics(result)
    assert result.returncode == 0, output
    assert "Fresh warning-policy-fixture" in output, output
    assert "Compiling warning-policy-fixture" not in output


def _assert_fixed_warning_rebuilds(cargo: str, target: str, crate: Path) -> None:
    """Require source repair to rebuild the fixture under denied warnings."""
    (crate / "src/lib.rs").write_text(FIXED_SOURCE, encoding="utf-8")
    result = _run_cargo(cargo, target, crate, "build", warnings="deny")
    output = _diagnostics(result)
    assert result.returncode == 0, output
    assert "Compiling warning-policy-fixture" in output, (
        "removing the warning under deny must rebuild the local package"
    )


@pytest.mark.timeout(CARGO_COMMAND_TIMEOUT_SECONDS)
def test_warning_policy_toggles_reuse_the_same_build_artifact(tmp_path: Path) -> None:
    """Show warn/deny changes replay warnings without recompiling the crate."""
    cargo = _require_toolchain()
    target = _host_target()
    crate = _crate(tmp_path, "warning-policy-fixture", WARNING_SOURCE)

    _assert_compiles_warning(cargo, target, crate)
    _assert_denial_reuses_artifact(cargo, target, crate)
    _assert_denial_reuses_artifact(cargo, target, crate)
    _assert_warning_recovery_reuses_artifact(cargo, target, crate)
    _assert_fixed_warning_rebuilds(cargo, target, crate)


@pytest.mark.timeout(CARGO_COMMAND_TIMEOUT_SECONDS)
def test_removing_rustflags_invalidates_an_otherwise_warm_build(
    tmp_path: Path,
) -> None:
    """Prove dropping the old warning flag causes only a one-off rebuild."""
    cargo = _require_toolchain()
    target = _host_target()
    crate = _crate(tmp_path, "flag-invalidation-fixture", FIXED_SOURCE)
    old_flags = f"-D warnings {BASE_RUSTFLAGS}"
    original = _run_cargo(
        cargo,
        target,
        crate,
        "build",
        warnings="warn",
        rustflags=old_flags,
    )
    original_output = _diagnostics(original)
    assert original.returncode == 0, original_output
    assert "Compiling flag-invalidation-fixture" in original_output

    removed_flag = _run_cargo(
        cargo,
        target,
        crate,
        "build",
        warnings="warn",
        rustflags=BASE_RUSTFLAGS,
    )
    changed_output = _diagnostics(removed_flag)
    assert removed_flag.returncode == 0, changed_output
    assert "Compiling flag-invalidation-fixture" in changed_output, (
        "removing the old -D warnings flag must invalidate its prior artifact once"
    )
    assert "deliberately_unused" not in changed_output, changed_output

    policy_only = _run_cargo(
        cargo, target, crate, "build", warnings="deny", rustflags=BASE_RUSTFLAGS
    )
    policy_output = _diagnostics(policy_only)
    assert policy_only.returncode == 0, policy_output
    assert "Fresh flag-invalidation-fixture" in policy_output, policy_output
    assert "Compiling flag-invalidation-fixture" not in policy_output


@pytest.mark.timeout(CARGO_COMMAND_TIMEOUT_SECONDS)
def test_clippy_warning_is_denied_and_replayed_from_a_warm_build(
    tmp_path: Path,
) -> None:
    """Cargo policy denial also covers a local Clippy warning on a fresh build."""
    cargo = _require_toolchain()
    target = _host_target()
    crate = _crate(tmp_path, "clippy-policy-fixture", CLIPPY_SOURCE)
    cold = _run_cargo(cargo, target, crate, "clippy")
    cold_output = _assert_fails_on_warning(
        cold, "clippy-policy-fixture", "needless_return"
    )
    assert "Checking clippy-policy-fixture" in cold_output or (
        "Compiling clippy-policy-fixture" in cold_output
    ), cold_output

    warm = _run_cargo(cargo, target, crate, "clippy")
    warm_output = _assert_fails_on_warning(
        warm, "clippy-policy-fixture", "needless_return"
    )
    assert "Fresh clippy-policy-fixture" in warm_output, warm_output
    assert "Checking clippy-policy-fixture" not in warm_output
    assert "Compiling clippy-policy-fixture" not in warm_output


@pytest.mark.timeout(CARGO_COMMAND_TIMEOUT_SECONDS)
def test_rustdoc_warning_policy_probe_records_its_observation(tmp_path: Path) -> None:
    """Record Cargo's effect on a rustdoc warning without claiming migration."""
    cargo = _require_toolchain()
    target = _host_target()
    crate = _crate(tmp_path, "rustdoc-policy-probe", RUSTDOC_SOURCE)
    environment = os.environ.copy()
    environment.pop("RUSTC", None)
    environment.pop("CARGO_ENCODED_RUSTFLAGS", None)
    environment.pop("RUSTDOCFLAGS", None)
    environment.update({
        "CARGO_BUILD_WARNINGS": "deny",
        "CARGO_TERM_COLOR": "never",
        "CARGO_TARGET_DIR": str(_target_dir(crate)),
        "RUSTFLAGS": BASE_RUSTFLAGS,
        "RUSTUP_TOOLCHAIN": MODERN_STABLE_TOOLCHAIN,
    })
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed Cargo argv.
        [
            cargo,
            "doc",
            "--locked",
            "--offline",
            "--verbose",
            "--target",
            target,
            "--profile",
            "dev",
        ],
        capture_output=True,
        check=False,
        cwd=crate,
        env=environment,
        timeout=CARGO_COMMAND_TIMEOUT_SECONDS,
        text=True,
    )
    output = _diagnostics(result)
    assert "broken intra-doc link" in output or "broken_intra_doc_links" in output, (
        output
    )
    build_warnings_denied_rustdoc = (
        "warnings are denied by `build.warnings` configuration" in output
    )
    RUSTDOC_PROBE_REPORT.parent.mkdir(parents=True, exist_ok=True)
    RUSTDOC_PROBE_REPORT.write_text(
        f"exit_code={result.returncode}\n"
        f"build_warnings_denied_rustdoc={build_warnings_denied_rustdoc}\n",
        encoding="utf-8",
    )


@pytest.mark.timeout(CARGO_COMMAND_TIMEOUT_SECONDS)
@pytest.mark.skipif(
    not shutil.which("sccache") or not os.environ.get("RUSTC_WRAPPER"),
    reason="a configured compiler wrapper is required for the wrapper variant",
)
def test_warning_policy_replay_works_through_the_configured_wrapper(
    tmp_path: Path,
) -> None:
    """Preserve warm warning replay when the environment uses sccache."""
    cargo = _require_toolchain()
    target = _host_target()
    crate = _crate(tmp_path, "wrapped-warning-fixture", WARNING_SOURCE)
    cold = _run_cargo(cargo, target, crate, "build", warnings="warn")
    cold_output = _diagnostics(cold)
    assert cold.returncode == 0, cold_output
    assert "Compiling wrapped-warning-fixture" in cold_output, cold_output

    warm = _run_cargo(cargo, target, crate, "build", warnings="deny")
    warm_output = _assert_fails_on_warning(
        warm, "wrapped-warning-fixture", "deliberately_unused"
    )
    assert "Fresh wrapped-warning-fixture" in warm_output, warm_output
    assert "Compiling wrapped-warning-fixture" not in warm_output


@pytest.mark.skipif(sys.platform != "linux", reason="the modern route runs in Linux CI")
def test_fixture_target_directory_stays_under_the_rust_workspace() -> None:
    """Keep test artefacts in the shared Cargo target, never under /tmp."""
    assert _target_dir(Path("/pytest-run/warning-policy-fixture")).is_relative_to(
        repo_root() / "rust/target"
    )
