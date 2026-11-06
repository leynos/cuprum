"""Drive the boundary fault harness through a recording command seam.

The recorder derives each result from the workspace it is handed rather than
from call order, so a harness that ran a control while a fault was planted — or
ran without planting anything — cannot be mistaken for a correct run. That
seam is shared with the fault catalogue's own tests, so it lives here.
"""

from __future__ import annotations

import typing as typ
from pathlib import Path

from scripts import boundary_fault_invocations
from scripts import check_boundary_faults as faults
from scripts.tests.boundary_harness_support import (
    _no_fault_preparation_time,
    copy_boundary_repository,
)

if typ.TYPE_CHECKING:
    import pytest

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

# Workspace-relative, because the harness copies the crate tree to its root.
MEMORY_IN_WORKSPACE = "cuprum-native-io/src/memory.rs"


class _RecordedRun:
    """Record every invocation while returning a result derived from the workspace.

    A control is identified by the workspace still holding production source; a
    fault is identified by the planted mutation. Deriving the result from the
    workspace rather than from call order keeps the seam honest: if the harness
    ran a control while a fault was planted, or ran without planting anything,
    the expected diagnostic would not match and the test would fail.
    """

    def __init__(self, workspace: Path) -> None:
        """Record the workspace under test and start with no calls."""
        self.workspace = workspace
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.target_dirs: list[Path | None] = []
        self.contexts: list[boundary_fault_invocations.VerifierContext] = []

    def __call__(
        self,
        check: faults._Check,
        workspace: Path,
        context: boundary_fault_invocations.VerifierContext,
    ) -> tuple[int, str]:
        """Record one invocation and return the result its workspace implies.

        Only the diagnostic that this specific check demands is returned, so a
        harness that accepted any failure — rather than the designated one —
        would still be caught.

        The bound context is recorded too: it is the value that decides which
        pins and repository the real run would have used, so a harness that
        bound the wrong one is an observable defect rather than an internal
        detail.

        Returns
        -------
        tuple[int, str]
            The exit code and output the harness will classify.
        """
        self.calls.append((check.program, check.args))
        self.target_dirs.append(check.target_dir)
        self.contexts.append(context)
        if check.target_dir is not None:
            # A real Cargo run populates the target directory it is given.
            # Creating it here keeps the harness's per-pair cleanup observable
            # instead of passing vacuously against an empty filesystem.
            check.target_dir.mkdir(parents=True, exist_ok=True)
        proof = Path(check.args[0])
        is_proof = proof.name == PROOF_TARGET
        if is_proof:
            source = proof.read_text(encoding="utf-8")
            planted = any(fragment in source for fragment in PROOF_FAULTS.values())
            rejected = VERUS_DIAGNOSTIC
        else:
            memory = (workspace / MEMORY_IN_WORKSPACE).read_text(encoding="utf-8")
            planted = any(fragment in memory for fragment in OWNERSHIP_FAULTS)
            rejected = CARGO_DIAGNOSTICS[check.args[0]]
        if not planted:
            return CONTROL_EXIT, f"{VERUS_DIAGNOSTIC}\n"
        return FAULT_EXIT, f"{rejected}\n"

    def names(self) -> list[str]:
        """Return the invocation programs in call order."""
        return [program for program, _args in self.calls]

    def proof_targets(self) -> list[str]:
        """Return the rendered proof file paths in call order."""
        # `Path.name` splits on the host's separator, so a recorded Windows
        # path still yields its filename rather than the whole path.
        return [Path(args[0]).name for _program, args in self.calls]


def drive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, _RecordedRun]:
    """Run the whole harness against a temporary repository copy.

    The copy is passed as ``main``'s root rather than patched into a module
    global: the run root, the workspace copy, and the tool pins all derive from
    that one argument, so they cannot disagree about which repository they
    belong to.

    Only the command seam is patched. ``_prepare_run_cargo_home`` keeps its
    real signature, because that signature is part of what the run binds: it
    receives the same root as everything else.

    Returns
    -------
    tuple[Path, _RecordedRun]
        The copied root the harness ran against, and the recorder holding every
        invocation it made.
    """
    root = copy_boundary_repository(tmp_path)
    runner = _RecordedRun(root / "rust")
    monkeypatch.setattr(faults, "_run", runner)
    monkeypatch.setattr(faults, "_prepare_run_cargo_home", _no_fault_preparation_time)
    faults.main(root)
    return root, runner
