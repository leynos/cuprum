"""Fault injection fails closed when the production correspondence changes.

The executable paths run against a temporary workspace copy through a recorded
command seam, so the tests assert what the harness would run, in which order,
and what it requires of each result — without launching Verus, Kani, or Cargo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import boundary_fault_invocations
from scripts import check_boundary_faults as faults
from scripts.check_boundary_faults import mutate
from scripts.tests.boundary_fault_recorder import (
    PROOF_FAULTS,
    _RecordedRun,
    drive,
)
from scripts.tests.boundary_harness_support import (
    copy_boundary_repository,
    latest_run_root,
    recorded_phases,
)

ROOT = Path(__file__).resolve().parents[2]

# Repository-relative, for reading the fixture root the harness is pointed at.
PROGRESS = "rust/cuprum-native-io/src/progress.rs"
MEMORY = "rust/cuprum-native-io/src/memory.rs"


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
    # Patching the re-exporting module would not reach the pin reader, which
    # resolves `ROOT` in the module that defines it.
    monkeypatch.setattr(boundary_fault_invocations, "ROOT", root)
    with pytest.raises(ValueError, match="records no version"):
        faults.read_tool_pins()


def test_a_missing_pin_is_a_hard_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent pin propagates rather than silently defaulting a version."""
    root = copy_boundary_repository(tmp_path)
    (root / "tools/verus/VERSION").unlink()
    monkeypatch.setattr(boundary_fault_invocations, "ROOT", root)
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
    root, runner = drive(tmp_path, monkeypatch)
    run_root = latest_run_root(root, "boundary-faults")
    workspace = run_root / "workspace"
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
    assert recorded_phases(run_root) == [
        "dependency_preparation",
        "workload",
    ], "preparation and fault execution must have separate timing records"


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
    build_root = logs.parent / "build"
    # Absent is the normal case once the last pair has cleaned up; present and
    # empty is equally fine. Anything left behind is scratch the pair failed to
    # remove.
    leftovers = list(build_root.iterdir()) if build_root.exists() else []
    assert not leftovers, (
        "each pair's scratch build directories must be removed once it finishes"
    )
    assert logs.is_dir(), "the archived log directory must survive the cleanup"
    assert any(logs.iterdir()), (
        "per-pair build cleanup must not touch the archived logs"
    )


def test_unexpected_verifier_results_archive_the_log_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unclassifiable result names the check and the archived log."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(
        faults,
        "_run",
        lambda _program, _args, _workspace, _target_dir=None: (3, "no verdict"),
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


def test_a_failing_pair_restores_the_source_and_cleans_up_its_build_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-pair `finally` runs when a check fails, not only on success."""
    root = copy_boundary_repository(tmp_path)
    production = (root / MEMORY).read_text(encoding="utf-8")
    logs = root / "logs"
    logs.mkdir()
    build_root = logs.parent / "build"
    monkeypatch.setattr(faults, "ROOT", root)

    # Populate each build tree the way a real Cargo run would, then fail the
    # pair from inside its second check so only the `finally` can clean up.
    def populate_then_fail(
        _program: str,
        _args: tuple[str, ...],
        _workspace: Path,
        target_dir: Path | None = None,
    ) -> tuple[int, str]:
        """Create the check's build tree, then report an unclassifiable result."""
        if target_dir is not None:
            target_dir.mkdir(parents=True, exist_ok=True)
        return 3, "no verdict"

    monkeypatch.setattr(faults, "_run", populate_then_fail)
    with pytest.raises(faults.BoundaryFaultError) as caught:
        faults._verify_ownership_faults(root / "rust", logs)

    assert caught.value.check_name == "leaked-writer-control", (
        "the failing pair must name the check that failed"
    )
    assert (root / MEMORY).read_text(encoding="utf-8") == production, (
        "a failed pair must still restore the copied source"
    )
    assert not (build_root / "leaked-writer-control").exists(), (
        "a failed pair must still remove its control build directory"
    )
    assert any(logs.iterdir()), "the failing pair's log must be archived"
    assert [path.name for path in sorted(logs.iterdir())] == [
        "leaked-writer-control.log"
    ], "only the attempted pair's log belongs in the archive"


def test_a_lockfile_rewrite_archives_output_before_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guarded run that rewrites its lockfile must still leave diagnostics."""
    logs = tmp_path / "logs"
    logs.mkdir()

    def rewrite(
        _program: str,
        _args: tuple[str, ...],
        _workspace: Path,
        _target_dir: Path | None = None,
    ) -> tuple[int, str]:
        """Fail the guarded run as though it had rewritten the lockfile."""
        error = faults._LockfileRewriteError(
            "changed the copied lockfile", "kani output"
        )
        raise error

    monkeypatch.setattr(faults, "_run", rewrite)
    with pytest.raises(faults._LockfileRewriteError):
        faults._check(
            faults._Check("kani-control", "cargo", ("kani", "--package", "x")),
            tmp_path,
            logs,
        )
    assert (logs / "kani-control.log").read_text(encoding="utf-8") == "kani output", (
        "the verifier output must be archived before the rewrite failure propagates"
    )


def test_a_control_that_fails_closed_is_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A control must pass, so a failing control cannot stand in for a fault."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(
        faults,
        "_run",
        lambda _program, _args, _workspace, _target_dir=None: (1, "unexpected failure"),
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
        faults,
        "_run",
        lambda _program, _args, _workspace, _target_dir=None: (
            1,
            "error: build failed",
        ),
    )
    with pytest.raises(faults.BoundaryFaultError) as caught:
        faults._check(check, tmp_path, logs)
    assert caught.value.check_name == "invalid-bound", "the check must be named"

    monkeypatch.setattr(
        faults,
        "_run",
        lambda _program, _args, _workspace, _target_dir=None: (
            1,
            "verification results::",
        ),
    )
    faults._check(check, tmp_path, logs)


def test_an_ownership_control_builds_apart_from_its_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control must not reuse Kani's prebuilt harness for the mutation.

    Kani decides whether to rebuild from the build directory alone. A control
    sharing one with its fault can therefore verify the mutation's prebuilt
    harness and archive a failed verification for production source, which is
    the defect that made this check flaky rather than sensitive.
    """
    root, runner = drive(tmp_path, monkeypatch)
    run_root = latest_run_root(root, "boundary-faults")
    harness = [
        target
        for (program, args), target in zip(
            runner.calls, runner.target_dirs, strict=True
        )
        if program == "cargo" and args[0] == "kani"
    ]
    assert len(harness) == 2, "the ownership fault must run one control and one fault"
    assert len(set(harness)) == 2, "the control must not share its fault's build dir"
    for target in harness:
        assert target is not None, (
            "both the control and its fault need their own build directory"
        )
        assert target.parent == run_root / "build", (
            "the per-check build directories must stay under the run's build root"
        )
        # CI archives the whole run root, `logs` included, so a Cargo target
        # tree under `logs` would ship build output as evidence on every run.
        assert not target.is_relative_to(run_root / "logs"), (
            "the build directories must stay out of the archived evidence tree"
        )
