"""Whole-run fault orchestration over a copied workspace.

The catalogue module pins which checks exist and what each one demands. These
tests take the run end to end: what ``main`` copies and binds, whether every
verifier run follows the root it was given, and whether the per-pair cleanup in
the ownership family runs on the failure path as well as the success path.

Every verifier result still arrives through the recorded command seam, so the
assertions are about orchestration rather than about Verus or Kani.
"""

from __future__ import annotations

import typing as typ

import pytest

from scripts import boundary_fault_invocations
from scripts import check_boundary_faults as faults
from scripts.tests import boundary_harness_support
from scripts.tests.boundary_fault_recorder import (
    MEMORY_IN_WORKSPACE,
    _RecordedRun,
    drive,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

MEMORY = "rust/cuprum-native-io/src/memory.rs"


def test_main_runs_both_fault_families_over_a_copied_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() copies the workspace and drives the proof and ownership faults."""
    root, runner = drive(tmp_path, monkeypatch)
    run_root = boundary_harness_support.latest_run_root(root, "boundary-faults")
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
    assert boundary_harness_support.recorded_phases(run_root) == [
        "dependency_preparation",
        "workload",
    ], "preparation and fault execution must have separate timing records"


def test_every_verifier_run_is_bound_to_the_repository_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run against a copy must not verify with the checkout's pins or root.

    The root decides both the workspace that is mutated and the tool versions
    the mutations are verified with. Binding the two separately would let a run
    plant a fault in the copy and then verify it with the checkout's Verus, so
    every recorded invocation has to carry the copy's root and the pins read
    from that same root.

    The copy's pins are overwritten first. Copying the repository brings the
    checkout's pins along, and equal pins would make the assertion below pass
    whichever root they were read from.
    """
    root = boundary_harness_support.copy_boundary_repository(tmp_path)
    (root / "tools/kani/VERSION").write_text("9.9.9\n", encoding="utf-8")
    (root / "tools/verus/VERSION").write_text("8.8.8\n", encoding="utf-8")
    runner = _RecordedRun(root / "rust")
    monkeypatch.setattr(faults, "_run", runner)
    boundary_harness_support.stub_fault_preparation(monkeypatch, faults)
    faults.main(root)

    assert len(runner.contexts) == len(runner.calls), (
        "every recorded invocation must carry the context it ran under"
    )
    assert {context.root for context in runner.contexts} == {root}, (
        "every verifier run must be bound to the copy's root"
    )
    assert {context.pins for context in runner.contexts} == {
        boundary_fault_invocations.ToolPins(kani="9.9.9", verus="8.8.8")
    }, "the pins must be read from the root the run is bound to, not the checkout"


def test_a_failing_pair_restores_the_source_and_cleans_up_its_build_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-pair `finally` runs when a check fails, not only on success."""
    root = boundary_harness_support.copy_boundary_repository(tmp_path)
    production = (root / MEMORY).read_text(encoding="utf-8")
    logs = root / "logs"
    logs.mkdir()
    build_root = logs.parent / "build"
    workspace = root / "rust"

    # Let the control pass so the harness reaches the mutation, then fail the
    # fault check. That order is what makes the assertions below non-vacuous:
    # an earlier failure would leave the source unmutated and the fault's
    # build tree uncreated, so restoring and removing them would prove nothing.
    #
    # Each check creates the build tree its real Cargo run would populate, so
    # cleanup has something to remove whichever one fails.
    def populate_then_fail_the_fault(
        check: faults._Check,
        _check_workspace: Path,
        _context: boundary_fault_invocations.VerifierContext,
    ) -> tuple[int, str]:
        """Create the check's build tree, passing the control and failing the fault."""
        if check.target_dir is None:
            return 0, "control passed"
        check.target_dir.mkdir(parents=True, exist_ok=True)
        if check.target_dir.name == "leaked-writer":
            return 3, "no verdict"
        return 0, "control passed"

    monkeypatch.setattr(faults, "_run", populate_then_fail_the_fault)
    with pytest.raises(faults.BoundaryFaultError) as caught:
        faults._verify_ownership_faults(
            boundary_harness_support.pinned_context(root), workspace, logs
        )

    assert caught.value.check_name == "leaked-writer", (
        "the failing pair must name the fault check that failed"
    )
    memory = (workspace / MEMORY_IN_WORKSPACE).read_text(encoding="utf-8")
    assert memory == production, "a failed pair must still restore the planted source"
    assert "core::mem::forget(writer)" not in memory, (
        "the planted fault must not survive the failed pair"
    )
    assert not (build_root / "leaked-writer-control").exists(), (
        "a failed pair must still remove its control build directory"
    )
    assert not (build_root / "leaked-writer").exists(), (
        "a failed pair must still remove the fault build directory"
    )
    assert [path.name for path in sorted(logs.iterdir())] == [
        "leaked-writer-control.log",
        "leaked-writer.log",
    ], "only the attempted pair's logs belong in the archive"


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
    run_root = boundary_harness_support.latest_run_root(root, "boundary-faults")
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
