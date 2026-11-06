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
from scripts.tests.boundary_fault_recorder import PROOF_FAULTS, _RecordedRun
from scripts.tests.boundary_harness_support import (
    copy_boundary_repository,
    pinned_context,
    stubbed_context,
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


def test_an_empty_pin_is_refused_rather_than_defaulted(tmp_path: Path) -> None:
    """A blank pin must fail closed instead of naming a verifier that cannot exist."""
    root = copy_boundary_repository(tmp_path)
    (root / "tools/kani/VERSION").write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="records no version"):
        faults.read_tool_pins(root)


def test_a_missing_pin_is_a_hard_failure(tmp_path: Path) -> None:
    """An absent pin propagates rather than silently defaulting a version."""
    root = copy_boundary_repository(tmp_path)
    (root / "tools/verus/VERSION").unlink()
    with pytest.raises(FileNotFoundError):
        faults.read_tool_pins(root)


def test_a_pin_read_uses_the_root_it_is_given(tmp_path: Path) -> None:
    """The pins follow the root argument, not the checkout they were imported from.

    A run against a copy must not silently verify with the checkout's pins.
    Recording distinct versions in the copy makes the two roots produce
    different answers, so reading the wrong one cannot pass.
    """
    root = copy_boundary_repository(tmp_path)
    (root / "tools/kani/VERSION").write_text("9.9.9\n", encoding="utf-8")
    (root / "tools/verus/VERSION").write_text("8.8.8\n", encoding="utf-8")

    pins = faults.read_tool_pins(root)

    assert (pins.kani, pins.verus) == ("9.9.9", "8.8.8"), (
        "the pins must be read from the root the caller named"
    )
    checkout = faults.read_tool_pins(ROOT)
    assert checkout != pins, (
        "a copy's pins must differ from the checkout's for this test to mean anything"
    )


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
    monkeypatch.setattr(faults, "_run", runner)
    monkeypatch.setattr(
        faults, "render", lambda source: rendered.append(source) or source
    )
    context = pinned_context(root)
    faults._verify_progress_faults(context, root / "rust", logs)

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
    monkeypatch.setattr(faults, "_run", runner)
    faults._verify_ownership_faults(pinned_context(root), root / "rust", logs)

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
        lambda _check, _workspace, _context: (3, "no verdict"),
    )
    with pytest.raises(faults.BoundaryFaultError) as caught:
        faults._check(
            faults._Check("compactness-control", "verus", ("proof.rs",)),
            stubbed_context(tmp_path),
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


def test_a_lockfile_rewrite_archives_output_before_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guarded run that rewrites its lockfile must still leave diagnostics."""
    logs = tmp_path / "logs"
    logs.mkdir()

    def rewrite(
        _check: faults._Check,
        _workspace: Path,
        _context: boundary_fault_invocations.VerifierContext,
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
            stubbed_context(tmp_path),
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
        lambda _check, _workspace, _context: (1, "unexpected failure"),
    )
    with pytest.raises(faults.BoundaryFaultError):
        faults._check(
            faults._Check("verus-control", "verus", ("proof.rs",)),
            stubbed_context(tmp_path),
            tmp_path,
            logs,
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
    context = stubbed_context(tmp_path)
    monkeypatch.setattr(
        faults,
        "_run",
        lambda _check, _workspace, _context: (
            1,
            "error: build failed",
        ),
    )
    with pytest.raises(faults.BoundaryFaultError) as caught:
        faults._check(check, context, tmp_path, logs)
    assert caught.value.check_name == "invalid-bound", "the check must be named"

    monkeypatch.setattr(
        faults,
        "_run",
        lambda _check, _workspace, _context: (
            1,
            "verification results::",
        ),
    )
    faults._check(check, context, tmp_path, logs)
