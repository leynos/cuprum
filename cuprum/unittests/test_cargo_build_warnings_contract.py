"""Prove Cargo warning policy changes preserve build freshness."""

from __future__ import annotations

import os
import shutil
import sys
import typing as typ
from pathlib import Path

import pytest

from tests.helpers.cargo_warning_fixture import (
    BASE_RUSTFLAGS,
    CARGO_COMMAND_TIMEOUT_SECONDS,
    TARGET_DIR,
    CargoFixture,
    diagnostics,
    host_target,
    make_crate,
    require_toolchain,
    target_dir,
)
from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import subprocess

RUSTDOC_PROBE_REPORT = TARGET_DIR / "rustdoc-warning-probe.txt"
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


@pytest.fixture(scope="session")
def modern_stable_toolchain() -> tuple[str, str]:
    """Probe the selected Cargo path and host triple once per test session."""
    return require_toolchain(), host_target()


def _fixture(toolchain: tuple[str, str], crate: Path) -> CargoFixture:
    """Bind the session toolchain to one fixture crate."""
    return CargoFixture(*toolchain, crate)


def _assert_fails_on_warning(
    result: subprocess.CompletedProcess[str], package: str, lint: str
) -> str:
    """Require warning denial to be the failure and return Cargo output."""
    output = diagnostics(result)
    assert result.returncode != 0, f"Cargo accepted {lint} under deny:\n{output}"
    assert lint in output, f"Cargo failed without reporting {lint}:\n{output}"
    assert (
        "error: warnings are denied by `build.warnings` configuration" in output
        or "error: could not compile" in output
    ), f"Cargo did not make {lint} fatal:\n{output}"
    assert package in output, f"Cargo did not identify the local package:\n{output}"
    return output


def _assert_compiles_warning(fixture: CargoFixture) -> None:
    """Require a warning-level build to compile and report the fixture lint."""
    result = fixture.run("build", warnings="warn")
    output = diagnostics(result)
    assert result.returncode == 0, output
    assert "Compiling warning-policy-fixture" in output, output
    assert "deliberately_unused" in output, output


def _assert_denial_reuses_artefact(fixture: CargoFixture) -> None:
    """Require a fresh Cargo replay to deny the cached fixture warning."""
    result = fixture.run("build", warnings="deny")
    output = _assert_fails_on_warning(
        result, "warning-policy-fixture", "deliberately_unused"
    )
    assert "Fresh warning-policy-fixture" in output, output
    assert "Compiling warning-policy-fixture" not in output, (
        "the policy-only warn-to-deny change must reuse the compiled crate"
    )


def _assert_warning_recovery_reuses_artefact(fixture: CargoFixture) -> None:
    """Require warning policy recovery to replay the cached build cleanly."""
    result = fixture.run("build", warnings="warn")
    output = diagnostics(result)
    assert result.returncode == 0, output
    assert "Fresh warning-policy-fixture" in output, output
    assert "Compiling warning-policy-fixture" not in output, (
        "warm warning denial must replay without recompiling the fixture"
    )


def _assert_fixed_warning_rebuilds(fixture: CargoFixture) -> None:
    """Require source repair to rebuild the fixture under denied warnings."""
    (fixture.crate / "src/lib.rs").write_text(FIXED_SOURCE, encoding="utf-8")
    result = fixture.run("build", warnings="deny")
    output = diagnostics(result)
    assert result.returncode == 0, output
    assert "Compiling warning-policy-fixture" in output, (
        "removing the warning under deny must rebuild the local package"
    )


@pytest.mark.timeout(CARGO_COMMAND_TIMEOUT_SECONDS)
def test_warning_policy_toggles_reuse_the_same_build_artefact(
    tmp_path: Path, modern_stable_toolchain: tuple[str, str]
) -> None:
    """Show warn/deny changes replay warnings without recompiling the crate."""
    fixture = _fixture(
        modern_stable_toolchain,
        make_crate(tmp_path, "warning-policy-fixture", WARNING_SOURCE),
    )

    _assert_compiles_warning(fixture)
    _assert_denial_reuses_artefact(fixture)
    _assert_denial_reuses_artefact(fixture)
    _assert_warning_recovery_reuses_artefact(fixture)
    _assert_fixed_warning_rebuilds(fixture)


@pytest.mark.timeout(CARGO_COMMAND_TIMEOUT_SECONDS)
def test_removing_rustflags_invalidates_an_otherwise_warm_build(
    tmp_path: Path, modern_stable_toolchain: tuple[str, str]
) -> None:
    """Prove dropping the old warning flag causes only a one-off rebuild."""
    fixture = _fixture(
        modern_stable_toolchain,
        make_crate(tmp_path, "flag-invalidation-fixture", FIXED_SOURCE),
    )
    old_flags = f"-D warnings {BASE_RUSTFLAGS}"
    original = fixture.run("build", warnings="warn", rustflags=old_flags)
    original_output = diagnostics(original)
    assert original.returncode == 0, original_output
    assert "Compiling flag-invalidation-fixture" in original_output, (
        "the initial Rustflags build must compile the fixture"
    )

    removed_flag = fixture.run("build", warnings="warn", rustflags=BASE_RUSTFLAGS)
    changed_output = diagnostics(removed_flag)
    assert removed_flag.returncode == 0, changed_output
    assert "Compiling flag-invalidation-fixture" in changed_output, (
        "removing the old -D warnings flag must invalidate its prior artefact once"
    )
    assert "deliberately_unused" not in changed_output, changed_output

    policy_only = fixture.run("build", warnings="deny", rustflags=BASE_RUSTFLAGS)
    policy_output = diagnostics(policy_only)
    assert policy_only.returncode == 0, policy_output
    assert "Fresh flag-invalidation-fixture" in policy_output, policy_output
    assert "Compiling flag-invalidation-fixture" not in policy_output, (
        "changing only warning policy must reuse the post-invalidation artefact"
    )


@pytest.mark.timeout(CARGO_COMMAND_TIMEOUT_SECONDS)
def test_clippy_warning_is_denied_and_replayed_from_a_warm_build(
    tmp_path: Path, modern_stable_toolchain: tuple[str, str]
) -> None:
    """Cargo policy denial also covers a local Clippy warning on a fresh build."""
    fixture = _fixture(
        modern_stable_toolchain,
        make_crate(tmp_path, "clippy-policy-fixture", CLIPPY_SOURCE),
    )
    cold = fixture.run("clippy")
    cold_output = _assert_fails_on_warning(
        cold, "clippy-policy-fixture", "needless_return"
    )
    assert "Checking clippy-policy-fixture" in cold_output or (
        "Compiling clippy-policy-fixture" in cold_output
    ), "the cold Clippy warning must be checked or compiled before denial"

    warm = fixture.run("clippy")
    warm_output = _assert_fails_on_warning(
        warm, "clippy-policy-fixture", "needless_return"
    )
    assert "Fresh clippy-policy-fixture" in warm_output, warm_output
    assert "Checking clippy-policy-fixture" not in warm_output, (
        "warm Clippy denial must not re-check the cached fixture"
    )
    assert "Compiling clippy-policy-fixture" not in warm_output, (
        "warm Clippy denial must not recompile the cached fixture"
    )


@pytest.mark.timeout(CARGO_COMMAND_TIMEOUT_SECONDS)
def test_rustdoc_warning_policy_probe_records_its_observation(
    tmp_path: Path, modern_stable_toolchain: tuple[str, str]
) -> None:
    """Record Cargo's effect on a rustdoc warning without claiming migration."""
    fixture = _fixture(
        modern_stable_toolchain,
        make_crate(tmp_path, "rustdoc-policy-probe", RUSTDOC_SOURCE),
    )
    result = fixture.run("doc")
    output = diagnostics(result)
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
    tmp_path: Path, modern_stable_toolchain: tuple[str, str]
) -> None:
    """Preserve warm warning replay when the environment uses sccache."""
    fixture = _fixture(
        modern_stable_toolchain,
        make_crate(tmp_path, "wrapped-warning-fixture", WARNING_SOURCE),
    )
    cold = fixture.run("build", warnings="warn")
    cold_output = diagnostics(cold)
    assert cold.returncode == 0, cold_output
    assert "Compiling wrapped-warning-fixture" in cold_output, cold_output

    warm = fixture.run("build", warnings="deny")
    warm_output = _assert_fails_on_warning(
        warm, "wrapped-warning-fixture", "deliberately_unused"
    )
    assert "Fresh wrapped-warning-fixture" in warm_output, warm_output
    assert "Compiling wrapped-warning-fixture" not in warm_output, (
        "the wrapped warm denial must replay without recompiling the fixture"
    )


@pytest.mark.skipif(sys.platform != "linux", reason="the modern route runs in Linux CI")
def test_fixture_target_directory_stays_under_the_rust_workspace() -> None:
    """Keep test artefacts in the shared Cargo target, never under /tmp."""
    assert target_dir(Path("/pytest-run/warning-policy-fixture")).is_relative_to(
        repo_root() / "rust/target"
    ), "Cargo fixtures must stay under the Rust workspace target directory"
