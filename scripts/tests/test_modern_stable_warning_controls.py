"""Check the modern-stable warning-control harness contracts."""

from __future__ import annotations

import pytest

from scripts.modern_stable_warning_controls import (
    GATES,
    WARNING_DENIAL,
    WARNING_FUNCTION,
    WARNING_REPLAY_STATE,
    GateRun,
    _append_warning,
    _has_warning_failure,
    _observe_output,
    _verify_prewarm,
    _verify_warm,
    _with_warning_policy,
)


def test_warning_injection_appends_one_private_unused_function() -> None:
    """Make the warning source explicit and reject accidental double edits."""
    source = "//! Fixture module.\n"

    changed = _append_warning(source)

    assert changed == f"{source}\n{WARNING_FUNCTION}\n", (
        "warning injection must append the single expected private function"
    )
    with pytest.raises(ValueError, match="already exists"):
        _append_warning(changed)


def test_controls_cover_each_migrated_gate_and_the_maturin_boundary() -> None:
    """Use the right Rust crate for every warning-policy propagation path."""
    assert tuple(gate.target for gate in GATES) == (
        "modern-stable-cargo-check",
        "modern-stable-clippy",
        "modern-stable-test",
        "modern-stable-develop",
    ), "the control harness must exercise all migrated gates and Maturin"
    assert all(
        gate.source.as_posix() == "rust/cuprum-streams/src/lib.rs" for gate in GATES[:3]
    ), "Cargo check, Clippy, and tests must inject the warning into cuprum-streams"
    assert all(gate.package == "cuprum-streams" for gate in GATES[:3]), (
        "the first three warning controls must target the cuprum-streams package"
    )
    assert GATES[-1].source.as_posix() == "rust/cuprum-rust/src/lib.rs", (
        "Maturin warning propagation must be checked in the extension crate"
    )
    assert GATES[-1].package == "cuprum-rust", (
        "the Maturin control must report the extension package"
    )


def test_pre_warmup_changes_only_the_single_modern_warning_setting() -> None:
    """Create a cached warning while retaining all other Makefile settings."""
    makefile = (
        "MODERN_STABLE_ENV = CARGO_BUILD_WARNINGS=deny "
        "CARGO_TARGET_DIR=rust/target/modern-stable\n"
    )

    assert _with_warning_policy(makefile, "warn") == makefile.replace(
        "CARGO_BUILD_WARNINGS=deny", "CARGO_BUILD_WARNINGS=warn"
    ), "pre-warmup must change only the modern Cargo warning policy"
    with pytest.raises(ValueError, match="exactly once"):
        _with_warning_policy(makefile + makefile, "warn")


def test_observation_counts_compile_and_fresh_lines() -> None:
    """Summarize Cargo freshness without discarding the original output."""
    output = (
        "    \x1b[1mFresh\x1b[0m proc-macro2 v1.0.0\n"
        "    \x1b[1mCompiling\x1b[0m cuprum-streams v0.1.0\n"
        "    \x1b[1mChecking\x1b[0m cuprum-rust v0.1.0\n"
    )

    result = _observe_output(GATES[0], "warning-warm", 101, output)

    assert result.exit_code == 101, (
        "the fixture must retain Cargo's warning-denial exit"
    )
    assert result.package_state == "Compiling", (
        "the package summary must report the compilation state"
    )
    assert result.compile_lines == 2, "both non-fresh package lines must be counted"
    assert result.fresh_lines == 1, "the cached dependency line must be counted"
    assert result.output == output, (
        "observation must preserve the complete Cargo output"
    )


def test_warning_prewarm_accepts_a_fresh_cached_package() -> None:
    """A cold denial may already populate the warning artefact for replay."""
    run = GateRun(
        gate="check",
        package="cuprum-streams",
        phase="warning-prewarm",
        exit_code=0,
        output=WARNING_FUNCTION,
        package_state="Fresh",
        compile_lines=0,
        fresh_lines=1,
    )
    failures: list[str] = []

    _verify_prewarm(run, failures)

    assert not failures, "a fresh cached warning prewarm must be accepted"


def test_warm_warning_replay_needs_no_local_fresh_line() -> None:
    """Cargo may replay a local warning without printing that crate as Fresh."""
    output = (
        "       Fresh proc-macro2 v1.0.0\n"
        f"warning: {WARNING_FUNCTION}\n"
        f"{WARNING_DENIAL}\n"
    )
    run = _observe_output(GATES[0], "warning-warm", 101, output)
    failures: list[str] = []

    _verify_warm(run, failures)

    assert run.package_state == WARNING_REPLAY_STATE, (
        "a cached warning denial must be distinguishable from a missing state"
    )
    assert run.compile_lines == 0, "the warm replay must not rebuild packages"
    assert run.fresh_lines == 1, "the run must retain positive cache freshness"
    assert not failures, "the cached warning replay must satisfy the warm control"


def test_warning_failure_requires_the_cargo_policy_diagnostic() -> None:
    """Reject failures that mention the marker without proving Cargo denied it."""
    failed_for_another_reason = GateRun(
        gate="check",
        package="cuprum-streams",
        phase="warning-cold",
        exit_code=101,
        output=WARNING_FUNCTION,
        package_state="Checking",
        compile_lines=1,
        fresh_lines=0,
    )
    denied_by_cargo = GateRun(
        gate="check",
        package="cuprum-streams",
        phase="warning-cold",
        exit_code=101,
        output=(
            f"warning: {WARNING_FUNCTION}\n"
            "error: warnings are denied by `build.warnings` configuration"
        ),
        package_state="Checking",
        compile_lines=1,
        fresh_lines=0,
    )

    assert not _has_warning_failure(failed_for_another_reason), (
        "a non-Cargo failure must not count as warning-policy evidence"
    )
    assert _has_warning_failure(denied_by_cargo), (
        "Cargo's build.warnings denial must count as warning-policy evidence"
    )
