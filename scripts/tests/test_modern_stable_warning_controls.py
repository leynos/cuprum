"""Check the modern-stable warning-control harness contracts."""

from __future__ import annotations

import typing as typ

import pytest

from scripts.modern_stable_warning_controls import (
    GATES,
    WARNING_FUNCTION,
    ControlHarness,
    _append_warning,
    _child_environment,
    _exercise_gates,
    _run_controls,
    _with_warning_policy,
    main,
)
from scripts.modern_stable_warning_observation import (
    WARNING_DENIAL,
    WARNING_REPLAY_STATE,
    GateRun,
    has_warning_failure,
    observe_output,
    verify_prewarm,
    verify_warm,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

POLICY_MAKEFILE = (
    "MODERN_STABLE_ENV = CARGO_BUILD_WARNINGS=deny "
    "CARGO_TARGET_DIR=rust/target/modern-stable\n"
)
FIXTURE_SOURCE = "//! Fixture module.\n"


def _write_worktree(worktree: pth.Path) -> None:
    """Write the Makefile and gate source a control run edits."""
    source = worktree / GATES[0].source
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(FIXTURE_SOURCE, encoding="utf-8")
    (worktree / "Makefile").write_text(POLICY_MAKEFILE, encoding="utf-8")


def _control_run(gate: str, phase: str) -> GateRun:
    """Return the result each phase of a healthy control run produces."""
    if phase == "warning-cold":
        return GateRun(
            gate,
            "cuprum-streams",
            phase,
            101,
            f"warning: {WARNING_FUNCTION}\n{WARNING_DENIAL}",
            "Checking",
            1,
            0,
        )
    if phase == "warning-prewarm":
        return GateRun(
            gate, "cuprum-streams", phase, 0, WARNING_FUNCTION, "Fresh", 0, 1
        )
    if phase == "clean":
        return GateRun(
            gate,
            "cuprum-streams",
            phase,
            0,
            "Compiling cuprum-streams",
            "Compiling",
            1,
            0,
        )
    return GateRun(
        gate,
        "cuprum-streams",
        phase,
        101,
        f"Fresh dep\nwarning: {WARNING_FUNCTION}\n{WARNING_DENIAL}",
        WARNING_REPLAY_STATE,
        0,
        1,
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


def test_child_environment_clears_inherited_make_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the outer Make's jobserver and recursion flags out of subprocesses."""
    monkeypatch.setenv("MAKEFLAGS", "--jobserver-auth=3,4 -j8")

    environment = _child_environment()

    assert not environment["MAKEFLAGS"], (
        "an inherited MAKEFLAGS must be cleared in the child environment"
    )
    assert environment["CARGO_BUILD_JOBS"] == "2", "Cargo builds must stay bounded"
    assert environment["CARGO_TERM_COLOR"] == "never", "logs must be plain text"
    assert environment["TEST_JOBS"] == "2", "test threads must stay bounded"


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

    result = observe_output(GATES[0], "warning-warm", 101, output)

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

    verify_prewarm(run, failures)

    assert not failures, "a fresh cached warning prewarm must be accepted"


def test_warm_warning_replay_needs_no_local_fresh_line() -> None:
    """Cargo may replay a local warning without printing that crate as Fresh."""
    output = (
        "       Fresh proc-macro2 v1.0.0\n"
        f"warning: {WARNING_FUNCTION}\n"
        f"{WARNING_DENIAL}\n"
    )
    run = observe_output(GATES[0], "warning-warm", 101, output)
    failures: list[str] = []

    verify_warm(run, failures)

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

    assert not has_warning_failure(failed_for_another_reason), (
        "a non-Cargo failure must not count as warning-policy evidence"
    )
    assert has_warning_failure(denied_by_cargo), (
        "Cargo's build.warnings denial must count as warning-policy evidence"
    )


def test_each_gate_runs_every_control_phase(tmp_path: pth.Path) -> None:
    """Exercise cold denial, prewarm, two warm replays, and a clean rerun."""
    worktree = tmp_path / "worktree"
    _write_worktree(worktree)
    phases: list[str] = []

    def run_gate(edited: pth.Path, gate: str, phase: str) -> GateRun:
        """Record the requested phase and answer with its expected result."""
        phases.append(phase)
        return _control_run(gate, phase)

    results, failures = _exercise_gates(worktree, (GATES[0],), run_gate)

    assert phases == [
        "warning-cold",
        "warning-prewarm",
        "warning-warm",
        "warning-warm-repeat",
        "clean",
    ], "each gate must run every control phase in order"
    assert [run.phase for run in results] == phases, (
        "the retained results must follow the phase order"
    )
    assert not failures, "a healthy control run must report no failures"


def test_a_gate_restores_the_policy_and_source_after_a_failure(
    tmp_path: pth.Path,
) -> None:
    """Leave the shared worktree usable when one phase raises."""

    def run_gate(edited: pth.Path, gate: str, phase: str) -> GateRun:
        """Fail during the first warm replay."""
        if phase == "warning-warm":
            message = "gate runner failed"
            raise RuntimeError(message)
        return _control_run(gate, phase)

    worktree = tmp_path / "worktree"
    _write_worktree(worktree)

    with pytest.raises(RuntimeError, match="gate runner failed"):
        _exercise_gates(worktree, (GATES[0],), run_gate)

    assert (worktree / "Makefile").read_text(encoding="utf-8") == POLICY_MAKEFILE, (
        "a failure must restore the checked-in warning denial"
    )
    assert (worktree / GATES[0].source).read_text(encoding="utf-8") == (
        FIXTURE_SOURCE
    ), "a failure must remove the injected warning from the source"


def test_a_worktree_setup_failure_is_reported_without_cleanup(
    tmp_path: pth.Path,
) -> None:
    """Report a worktree that was never created and skip removal."""

    def forbidden(path: pth.Path, root: pth.Path) -> str | None:
        """Fail the test if cleanup runs for a worktree never created."""
        message = f"cleanup must not run for {path} under {root}"
        raise AssertionError(message)

    harness = ControlHarness(
        add_worktree=lambda path, root: "git worktree add failed (1): boom",
        remove_worktree=forbidden,
    )

    results, failures = _run_controls(root=tmp_path, harness=harness)

    assert not results, "a setup failure must not report gate results"
    assert failures == ("git worktree add failed (1): boom",), (
        "the setup failure must be the reported cause"
    )


def test_cleanup_runs_and_its_failure_is_aggregated(tmp_path: pth.Path) -> None:
    """Remove the created worktree and report a cleanup fault separately."""

    def add_worktree(path: pth.Path, root: pth.Path) -> str | None:
        """Create the worktree contents the exercise phase edits."""
        _write_worktree(path)

    removed: list[pth.Path] = []

    def remove_worktree(path: pth.Path, root: pth.Path) -> str | None:
        """Record the removal and report a cleanup fault."""
        removed.append(path)
        return "git worktree remove failed (1): boom"

    harness = ControlHarness(
        gates=(GATES[0],),
        add_worktree=add_worktree,
        remove_worktree=remove_worktree,
        run_gate=lambda worktree, gate, phase, root: _control_run(gate, phase),
    )

    results, failures = _run_controls(root=tmp_path, harness=harness)

    assert len(removed) == 1, "the created worktree must be removed exactly once"
    assert len(results) == 5, "the healthy gate must retain all five phase results"
    assert failures == ("git worktree remove failed (1): boom",), (
        "a cleanup fault must be reported alongside the gate results"
    )


@pytest.mark.parametrize(
    ("setup_failure", "expected_exit"),
    [(None, 0), ("git worktree add failed (1): boom", 1)],
)
def test_main_returns_its_status_and_retains_the_report(
    tmp_path: pth.Path, setup_failure: str | None, expected_exit: int
) -> None:
    """Prove a complete control exits zero and a failed one exits non-zero."""

    def add_worktree(path: pth.Path, root: pth.Path) -> str | None:
        """Create the worktree contents unless the case tests a setup fault."""
        if setup_failure is None:
            _write_worktree(path)
        return setup_failure

    harness = ControlHarness(
        gates=(GATES[0],),
        add_worktree=add_worktree,
        remove_worktree=lambda path, root: None,
        run_gate=lambda worktree, gate, phase, root: _control_run(gate, phase),
    )

    exit_code = main(root=tmp_path, harness=harness)

    report = (
        tmp_path / "rust/target/modern-stable-warning-controls/report.md"
    ).read_text(encoding="utf-8")
    assert exit_code == expected_exit, (
        "main must return zero only when every control phase succeeds"
    )
    assert ("Failures:\n\n- none" in report) is (setup_failure is None), (
        "the retained report must state whether any control failed"
    )
