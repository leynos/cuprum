"""Check the warning-control entry point and its worktree lifecycle.

The orchestration tests exercise the individual boundaries. These tests cover
the command that wires those boundaries together: which failures it reports,
which worktrees it removes, and what exit status and report it retains.
"""

from __future__ import annotations

import typing as typ

import pytest

from scripts.modern_stable_warning_controls import (
    GATES,
    ControlHarness,
    _run_controls,
    main,
)
from scripts.modern_stable_warning_observation import Gate, GateRun
from scripts.tests.modern_stable_warning_controls_support import (
    CONTROL_PHASES,
    control_run,
    write_worktree,
)

if typ.TYPE_CHECKING:
    import pathlib as pth


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
        write_worktree(path)

    removed: list[pth.Path] = []

    def remove_worktree(path: pth.Path, root: pth.Path) -> str | None:
        """Record the removal and report a cleanup fault."""
        removed.append(path)
        return "git worktree remove failed (1): boom"

    harness = ControlHarness(
        gates=(GATES[0],),
        add_worktree=add_worktree,
        remove_worktree=remove_worktree,
        run_gate=lambda worktree, gate, phase, root: control_run(gate.name, phase),
    )

    results, failures = _run_controls(root=tmp_path, harness=harness)

    assert len(removed) == 1, "the created worktree must be removed exactly once"
    assert len(results) == len(CONTROL_PHASES), (
        "the healthy gate must retain every phase result"
    )
    assert failures == ("git worktree remove failed (1): boom",), (
        "a cleanup fault must be reported alongside the gate results"
    )


@pytest.mark.parametrize(
    ("setup_failure", "cold_exit", "expected_exit"),
    [
        (None, 101, 0),
        ("git worktree add failed (1): boom", 101, 1),
        (None, 0, 1),
    ],
    ids=["complete", "setup fault", "failed control"],
)
def test_main_returns_its_status_and_retains_the_report(
    tmp_path: pth.Path,
    setup_failure: str | None,
    cold_exit: int,
    expected_exit: int,
) -> None:
    """Prove a complete control exits zero and a failed one exits non-zero."""

    def add_worktree(path: pth.Path, root: pth.Path) -> str | None:
        """Create the worktree contents unless the case tests a setup fault."""
        if setup_failure is None:
            write_worktree(path)
        return setup_failure

    def run_gate(worktree: pth.Path, gate: Gate, phase: str) -> GateRun:
        """Answer the cold phase with the run this case configures."""
        if phase == "warning-cold" and cold_exit == 0:
            return GateRun(gate.name, gate.package, phase, 0, "", "Checking", 1, 0)
        return control_run(gate.name, phase)

    harness = ControlHarness(
        gates=(GATES[0],),
        add_worktree=add_worktree,
        remove_worktree=lambda path, root: None,
        run_gate=lambda worktree, gate, phase, root: run_gate(worktree, gate, phase),
    )

    exit_code = main(root=tmp_path, harness=harness)

    report = (
        tmp_path / "rust/target/modern-stable-warning-controls/report.md"
    ).read_text(encoding="utf-8")
    assert exit_code == expected_exit, (
        "main must return zero only when every control phase succeeds"
    )
    assert ("Failures:\n\n- none" in report) is (expected_exit == 0), (
        "the retained report must state whether any control failed"
    )
    if setup_failure is None and cold_exit == 0:
        assert "cold run did not compile and fail on the warning" in report, (
            "the report must retain the diagnostic of the phase that failed"
        )
