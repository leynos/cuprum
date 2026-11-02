"""Check the modern-stable warning-control harness contracts."""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import TimeoutExpired
from scripts.modern_stable_warning_controls import (
    GATE_TIMEOUT_EXIT_CODE,
    GATE_TIMEOUT_SECONDS,
    GATES,
    WARNING_FUNCTION,
    _append_warning,
    _child_environment,
    _exercise_gates,
    _run,
    _with_warning_policy,
)
from scripts.modern_stable_warning_observation import (
    WARNING_DENIAL,
    WARNING_REPLAY_STATE,
    Gate,
    GateRun,
    has_warning_failure,
    observe_output,
    verify_prewarm,
    verify_warm,
)
from scripts.tests.modern_stable_warning_controls_support import (
    CONTROL_PHASES,
    FIXTURE_SOURCE,
    POLICY_MAKEFILE,
    control_run,
    write_worktree,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

    from cuprum.sh import SafeCmdBuilder


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


class _ExpiredCommand:
    """Raise the timeout the real command raises, without starting a process."""

    def run_sync(self, *, context: object) -> typ.NoReturn:
        """Fail the way a command that outlived its bound fails."""
        message = f"expired under {context}"
        raise TimeoutExpired(cmd=["make"], timeout=GATE_TIMEOUT_SECONDS, output=message)


class _ExpiredProgram:
    """Stand in for a command builder whose run outlives its bound."""

    def __call__(self, *_arguments: str) -> _ExpiredCommand:
        """Return a command that expires instead of running."""
        return _ExpiredCommand()


def test_a_gate_that_outlives_its_bound_is_a_failure(tmp_path: pth.Path) -> None:
    """Report an expired gate as a failure instead of waiting forever."""
    program = typ.cast("SafeCmdBuilder", _ExpiredProgram())

    exit_code, output = _run(program, ("-C", "worktree", "target"), tmp_path)

    assert exit_code == GATE_TIMEOUT_EXIT_CODE, (
        "an expired gate must report a failure exit status"
    )
    assert "expired under" in output, "captured output must survive the expiry"
    assert "timed out after" in output, "the log must name the expiry"


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


def test_each_configured_gate_runs_every_control_phase(tmp_path: pth.Path) -> None:
    """Exercise every phase for each configured gate, in gate and phase order."""
    worktree = tmp_path / "worktree"
    write_worktree(worktree)
    gates = (GATES[0], GATES[-1])
    observed: list[tuple[str, str]] = []

    def run_gate(edited: pth.Path, gate: Gate, phase: str) -> GateRun:
        """Record the requested gate and phase, and answer as expected."""
        observed.append((gate.name, phase))
        return control_run(gate.name, phase)

    results, failures = _exercise_gates(worktree, gates, run_gate)

    assert observed == [
        (gate.name, phase) for gate in gates for phase in CONTROL_PHASES
    ], "each configured gate must run every control phase in order"
    assert [(run.gate, run.phase) for run in results] == observed, (
        "the retained results must follow the gate and phase order"
    )
    assert not failures, "a healthy control run must report no failures"


def test_a_gate_restores_the_policy_and_source_after_a_failure(
    tmp_path: pth.Path,
) -> None:
    """Leave the shared worktree usable when one phase raises."""

    def run_gate(edited: pth.Path, gate: Gate, phase: str) -> GateRun:
        """Fail during the first warm replay."""
        if phase == "warning-warm":
            message = "gate runner failed"
            raise RuntimeError(message)
        return control_run(gate.name, phase)

    worktree = tmp_path / "worktree"
    write_worktree(worktree)

    with pytest.raises(RuntimeError, match="gate runner failed"):
        _exercise_gates(worktree, (GATES[0],), run_gate)

    assert (worktree / "Makefile").read_text(encoding="utf-8") == POLICY_MAKEFILE, (
        "a failure must restore the checked-in warning denial"
    )
    assert (worktree / GATES[0].source).read_text(encoding="utf-8") == (
        FIXTURE_SOURCE
    ), "a failure must remove the injected warning from the source"
