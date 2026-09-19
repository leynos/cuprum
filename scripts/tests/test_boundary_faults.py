"""Fault injection fails closed when the production correspondence changes.

The executable paths run against a temporary workspace copy through a recorded
command seam, so the tests assert what the harness would run, in which order,
and what it requires of each result — without launching Verus, Kani, or Cargo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import check_boundary_faults as faults
from scripts.check_boundary_faults import mutate
from scripts.tests.boundary_harness_support import copy_boundary_repository

ROOT = Path(__file__).resolve().parents[2]

# Repository-relative, for reading the fixture root the harness is pointed at.
PROGRESS = "rust/cuprum-native-io/src/progress.rs"
MEMORY = "rust/cuprum-native-io/src/memory.rs"
# Workspace-relative, because the harness copies the crate tree to its root.
MEMORY_IN_WORKSPACE = "cuprum-native-io/src/memory.rs"

# What the harness archives each proof fault under, and the fragments that must
# only ever appear in a rendered fault.
PROOF_FAULTS = {
    "invalid-bound": "count >= capacity",
    "incorrect-accounting": "total - written",
}

# Fragments proving an ownership fault is currently planted in the workspace.
OWNERSHIP_FAULTS = ("core::mem::forget(writer)", "core::mem::forget(retained)")

# Exit codes chosen to be distinct, so a control that returned a fault's code
# would not have run and a fault that returned a control's code was not caught.
CONTROL_EXIT = 0
FAULT_EXIT = 7

# The diagnostic each verifier emits for a rejected check. Only the demanded
# one is reported, so a harness that accepted any failure would fail.
#
# A rendered proof is checked by Verus (its `program` is the absolute verus
# path). An ownership fault is a Cargo run, and which diagnostic it must
# produce depends on the subcommand: `cargo kani` reports a failed
# verification, while `cargo test` reports a failed test result.
VERUS_DIAGNOSTIC = "verification results::"
KANI_DIAGNOSTIC = "VERIFICATION:- FAILED"
TEST_DIAGNOSTIC = "test result: FAILED"
CARGO_DIAGNOSTICS = {"kani": KANI_DIAGNOSTIC, "test": TEST_DIAGNOSTIC}
PROOF_TARGET = "progress-fault.rs"


class _RecordedRun:
    """Record every invocation while returning a result derived from the workspace.

    A control is identified by the workspace still holding production source; a
    fault is identified by the planted mutation. Deriving the result from the
    workspace rather than from call order keeps the seam honest: if the harness
    ran a control while a fault was planted, or ran without planting anything,
    the expected diagnostic would not match and the test would fail.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def __call__(
        self, program: str, args: tuple[str, ...], workspace: Path
    ) -> tuple[int, str]:
        """Record one invocation and return the result its workspace implies.

        Only the diagnostic that this specific check demands is returned, so a
        harness that accepted any failure — rather than the designated one —
        would still be caught.

        Returns
        -------
        tuple[int, str]
            The exit code and output the harness will classify.
        """
        self.calls.append((program, args))
        proof = Path(args[0])
        is_proof = proof.name == PROOF_TARGET
        if is_proof:
            source = proof.read_text(encoding="utf-8")
            planted = any(fragment in source for fragment in PROOF_FAULTS.values())
            rejected = VERUS_DIAGNOSTIC
        else:
            memory = (workspace / MEMORY_IN_WORKSPACE).read_text(encoding="utf-8")
            planted = any(fragment in memory for fragment in OWNERSHIP_FAULTS)
            rejected = CARGO_DIAGNOSTICS[args[0]]
        if not planted:
            return CONTROL_EXIT, f"{VERUS_DIAGNOSTIC}\n"
        return FAULT_EXIT, f"{rejected}\n"

    def names(self) -> list[str]:
        """Return the invocation programs in call order."""
        return [program for program, _args in self.calls]

    def proof_targets(self) -> list[str]:
        """Return the rendered proof file paths in call order."""
        return [args[0].rsplit("/", 1)[-1] for _program, args in self.calls]


def _drive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, _RecordedRun]:
    """Run the whole harness against a temporary repository copy."""
    root = copy_boundary_repository(tmp_path)
    runner = _RecordedRun(root / "rust")
    monkeypatch.setattr(faults, "ROOT", root)
    monkeypatch.setattr(faults, "_run", runner)
    faults.main()
    return root, runner


def test_the_harness_launches_the_pinned_installer_versions() -> None:
    """A drifted pin must fail here rather than at verifier launch."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    pins = faults.read_tool_pins()
    assert f"KANI_VERSION = {pins.kani}" in makefile, (
        "the harness Kani pin drifted from the Makefile's installer"
    )
    assert f"cuprum-verus-{pins.verus}" in makefile, (
        "the harness Verus root drifted from the Makefile's installer"
    )


def test_every_pin_is_read_from_its_manifest() -> None:
    """Pins come from the tool manifests and are never blank."""
    pins = faults.read_tool_pins()
    for name, version in (("kani", pins.kani), ("verus", pins.verus)):
        expected = (ROOT / f"tools/{name}/VERSION").read_text(encoding="utf-8").strip()
        assert version == expected != "", f"the {name} pin is not the manifest version"


def test_an_empty_pin_is_refused_rather_than_defaulted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank pin must fail closed instead of naming a verifier that cannot exist."""
    root = copy_boundary_repository(tmp_path)
    (root / "tools/kani/VERSION").write_text("\n", encoding="utf-8")
    monkeypatch.setattr(faults, "ROOT", root)
    with pytest.raises(ValueError, match="records no version"):
        faults.read_tool_pins()


def test_a_missing_pin_is_a_hard_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent pin propagates rather than silently defaulting a version."""
    root = copy_boundary_repository(tmp_path)
    (root / "tools/verus/VERSION").unlink()
    monkeypatch.setattr(faults, "ROOT", root)
    with pytest.raises(FileNotFoundError):
        faults.read_tool_pins()


@pytest.mark.parametrize("source", ["absent", "old old"])
def test_fault_rejects_missing_or_ambiguous_kernel(source: str) -> None:
    """A stale mutation must fail rather than claiming harness sensitivity."""
    with pytest.raises(ValueError, match="exactly one"):
        mutate(source, "old", "new")


def test_fault_changes_only_the_named_fragment() -> None:
    """Surrounding executable code remains the production implementation."""
    assert mutate("before old after", "old", "new") == "before new after", (
        "fault changed surrounding production code"
    )


def test_main_runs_both_fault_families_over_a_copied_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() copies the workspace and drives the proof and ownership faults."""
    root, runner = _drive(tmp_path, monkeypatch)
    workspace = root / ".cache/boundary-faults/workspace"
    assert workspace.is_dir(), "the harness must copy the workspace before mutating"
    assert not (workspace / "target").exists(), (
        "the workspace copy must exclude Cargo build output"
    )
    # One control plus the two progress faults, and a control plus the fault for
    # each of the two ownership faults.
    assert runner.proof_targets().count("progress-fault.rs") == 3, (
        "each progress fault must be preceded by a control, plus the baseline"
    )
    assert runner.names().count("cargo") == 4, (
        "each ownership fault must be preceded by a control"
    )


def test_every_proof_fault_reaches_the_verus_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault is only detected if the mutation survives rendering into the proof."""
    root = copy_boundary_repository(tmp_path)
    production = (root / PROGRESS).read_text(encoding="utf-8")
    rendered: list[str] = []
    runner = _RecordedRun(root / "rust")
    logs = root / "logs"
    logs.mkdir()
    monkeypatch.setattr(faults, "ROOT", root)
    monkeypatch.setattr(faults, "_run", runner)
    monkeypatch.setattr(
        faults, "render", lambda source: rendered.append(source) or source
    )
    faults._verify_progress_faults(root / "rust", logs)

    for name, fragment in PROOF_FAULTS.items():
        assert any(fragment in source for source in rendered), (
            f"the {name} fault never reached the Verus input"
        )
    assert (root / PROGRESS).read_text(encoding="utf-8") == production, (
        "the proof fault was not restored after checking"
    )


def test_ownership_faults_are_detected_and_their_mutations_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both ownership faults are caught, and the workspace keeps production source."""
    root = copy_boundary_repository(tmp_path)
    production = (root / MEMORY).read_text(encoding="utf-8")
    runner = _RecordedRun(root / "rust")
    logs = root / "logs"
    logs.mkdir()
    monkeypatch.setattr(faults, "ROOT", root)
    monkeypatch.setattr(faults, "_run", runner)
    faults._verify_ownership_faults(root / "rust", logs)

    # Each fault logs its control and the fault itself, in that order: the
    # control proves the check passes on production, and the fault's own log
    # proves the mutation was planted before it was required to fail.
    assert [path.name for path in sorted(logs.iterdir())] == [
        "leaked-writer-control.log",
        "leaked-writer.log",
        "trailing-forget-unwind-control.log",
        "trailing-forget-unwind.log",
    ], "each ownership fault must run its control first"
    assert (root / MEMORY).read_text(encoding="utf-8") == production, (
        "the ownership fault was not restored after checking"
    )


def test_unexpected_verifier_results_archive_the_log_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unclassifiable result names the check and the archived log."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(
        faults, "_run", lambda _program, _args, _workspace: (3, "no verdict")
    )
    with pytest.raises(faults.BoundaryFaultError) as caught:
        faults._check(
            faults._Check("compactness-control", "verus", ("proof.rs",)),
            tmp_path,
            logs,
        )
    assert caught.value.check_name == "compactness-control", "the check must be named"
    assert caught.value.exit_code == 3, "the verifier exit code must be retained"
    assert caught.value.log_path == logs / "compactness-control.log", (
        "the archived log path must be reported"
    )
    assert "no verdict" in caught.value.log_path.read_text(encoding="utf-8"), (
        "the verifier output must be archived for inspection"
    )


def test_a_control_that_fails_closed_is_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A control must pass, so a failing control cannot stand in for a fault."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(
        faults, "_run", lambda _program, _args, _workspace: (1, "unexpected failure")
    )
    with pytest.raises(faults.BoundaryFaultError):
        faults._check(
            faults._Check("verus-control", "verus", ("proof.rs",)), tmp_path, logs
        )


def test_a_fault_is_only_accepted_for_its_own_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrelated failure must not satisfy a check that names a diagnostic."""
    logs = tmp_path / "logs"
    logs.mkdir()
    check = faults._Check(
        "invalid-bound", "verus", ("proof.rs",), "verification results::"
    )
    monkeypatch.setattr(
        faults, "_run", lambda _program, _args, _workspace: (1, "error: build failed")
    )
    with pytest.raises(faults.BoundaryFaultError) as caught:
        faults._check(check, tmp_path, logs)
    assert caught.value.check_name == "invalid-bound", "the check must be named"

    monkeypatch.setattr(
        faults,
        "_run",
        lambda _program, _args, _workspace: (1, "verification results::"),
    )
    faults._check(check, tmp_path, logs)
