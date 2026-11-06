#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.13"
# dependencies = ["cuprum==0.1.0"]
# ///
"""Require representative production faults to fail their designated checks.

All mutations affect a disposable workspace copy. Logs retain counterexamples
and real-unwind failures, and a successful control precedes each mutation.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from scripts.boundary_cargo import (
    boundary_cargo_run as _boundary_cargo_run,
)
from scripts.boundary_cargo import (
    new_run_root as _new_boundary_run_root,
)
from scripts.boundary_cargo import (
    prepare_run_cargo_home as _prepare_boundary_cargo_home,
)
from scripts.boundary_fault_invocations import (
    ROOT,
    _Check,
    _run,
    read_tool_pins,
)
from scripts.boundary_fault_invocations import (
    _LockfileRewriteError as _LockfileRewriteError,
)
from scripts.render_boundary_proofs import render


class BoundaryFaultError(RuntimeError):
    """A verifier result the fault harness cannot classify.

    Attributes
    ----------
    check_name : str
        Name of the check whose verifier result was unexpected.
    exit_code : int
        Exit code returned by the verifier.
    log_path : Path
        Archived verifier output for the unexpected result.
    """

    def __init__(self, check_name: str, exit_code: int, log_path: Path) -> None:
        """Record the check identity, exit code, and archived log path."""
        msg = f"unexpected fault-sensitivity result: {check_name}; inspect {log_path}"
        super().__init__(msg)
        self.check_name = check_name
        self.exit_code = exit_code
        self.log_path = log_path


def mutate(source: str, old: str, new: str) -> str:
    """Apply exactly one fault, failing closed if production has changed.

    Parameters
    ----------
    source : str
        Actual production source text.
    old : str
        Unique implementation fragment to replace.
    new : str
        Deliberately incorrect implementation.

    Returns
    -------
    str
        Mutated source with exactly one replacement.

    Raises
    ------
    ValueError
        If the implementation fragment is absent or ambiguous.
    """
    if source.count(old) != 1:
        msg = "fault no longer matches exactly one production fragment"
        raise ValueError(msg)
    return source.replace(old, new)


def _check(
    check: _Check,
    workspace: Path,
    logs: Path,
    target_dir: Path | None = None,
) -> None:
    """Archive a control or require the specific counterexample diagnostic."""
    log_path = logs / f"{check.name}.log"
    try:
        code, output = _run(check.program, check.args, workspace, target_dir)
    except _LockfileRewriteError as error:
        log_path.write_text(error.output, encoding="utf-8")
        raise
    log_path.write_text(output, encoding="utf-8")
    passed = (
        code == 0
        if check.expected_failure is None
        else code != 0 and check.expected_failure in output
    )
    if not passed:
        raise BoundaryFaultError(check.name, code, log_path)


def main() -> None:
    """Execute controls and four deliberate faults without editing live sources."""
    run_root = _new_run_root()
    workspace = run_root / "workspace"
    shutil.copytree(ROOT / "rust", workspace, ignore=shutil.ignore_patterns("target"))
    with _boundary_cargo_run(run_root, workspace, _prepare_run_cargo_home):
        logs = run_root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        _verify_progress_faults(workspace, logs)
        _verify_ownership_faults(workspace, logs)


def _new_run_root() -> Path:
    """Create a private workspace root for one fault-sensitivity run."""
    return _new_boundary_run_root(ROOT / ".cache/boundary-faults/runs")


def _prepare_run_cargo_home(run_root: Path, workspace: Path) -> float:
    """Prepare the copied workspace with this harness's repository root."""
    return _prepare_boundary_cargo_home(ROOT, run_root, workspace)


def _verify_progress_faults(workspace: Path, logs: Path) -> None:
    """Check the production progress kernels against bound and accounting faults."""
    pins = read_tool_pins()
    install = Path(
        os.environ.get(
            "VERUS_INSTALL_DIR",
            str(Path.home() / f".local/share/cuprum-verus-{pins.verus}"),
        )
    )
    verus = str(install / "verus/verus")
    progress = (workspace / "cuprum-native-io/src/progress.rs").read_text(
        encoding="utf-8"
    )
    proof = workspace / "progress-fault.rs"
    proof.write_text(render(progress), encoding="utf-8")
    args = (str(proof), "--crate-type=lib")
    _check(_Check("verus-control", verus, args), workspace, logs)
    for name, old, new in (
        ("invalid-bound", "count <= capacity", "count >= capacity"),
        ("incorrect-accounting", "total + written", "total - written"),
    ):
        proof.write_text(render(mutate(progress, old, new)), encoding="utf-8")
        _check(
            _Check(name, verus, args, "verification results::"),
            workspace,
            logs,
        )


def _verify_ownership_faults(workspace: Path, logs: Path) -> None:
    """Check writer drop and real-unwind retention faults against their controls."""
    memory = workspace / "cuprum-native-io/src/memory.rs"
    original = memory.read_text(encoding="utf-8")
    ownership = (
        "kani",
        "--package",
        "cuprum-native-io",
        "--harness",
        "production_scope_preserves_reader_and_drops_writer",
    )
    unwind = (
        "test",
        "--package",
        "cuprum-native-io",
        "--lib",
        "memory::tests::retained_owner_survives_real_unwind",
        "--",
        "--exact",
    )
    # The control and its fault get separate build directories. Sharing one
    # lets Kani decide the unchanged control needs no rebuild and then report
    # the mutation's prebuilt harness, so a working control is archived as a
    # failed verification.
    #
    # They live beside the run's own workspace, in the run's `build` directory,
    # rather than under `logs`. CI archives the run root wholesale with
    # `include-hidden-files`, so keeping a Cargo target tree out of `logs` keeps
    # the archived evidence readable rather than shipping build output with it.
    build_root = logs.parent / "build"
    for name, args, old, new, diagnostic in (
        (
            "leaked-writer",
            ownership,
            "    drop(writer);",
            "    core::mem::forget(writer);",
            "VERIFICATION:- FAILED",
        ),
        (
            "trailing-forget-unwind",
            unwind,
            (
                "    let mut retained = core::mem::ManuallyDrop::new(value);\n"
                "    operation(&mut retained)"
            ),
            (
                "    let mut retained = value;\n"
                "    let result = operation(&mut retained);\n"
                "    core::mem::forget(retained);\n    result"
            ),
            "test result: FAILED",
        ),
    ):
        try:
            _check(
                _Check(f"{name}-control", "cargo", args),
                workspace,
                logs,
                build_root / f"{name}-control",
            )
            memory.write_text(mutate(original, old, new), encoding="utf-8")
            _check(
                _Check(name, "cargo", args, diagnostic),
                workspace,
                logs,
                build_root / name,
            )
        finally:
            # Restore the source first so a cleanup failure cannot leave the
            # copied workspace holding a planted fault. The two build trees are
            # scratch: their verdicts are already archived under `logs`, and
            # they exist only to keep the control from reusing the mutation's
            # prebuilt harness.
            memory.write_text(original, encoding="utf-8")
            shutil.rmtree(build_root / f"{name}-control", ignore_errors=True)
            shutil.rmtree(build_root / name, ignore_errors=True)


if __name__ == "__main__":
    main()
