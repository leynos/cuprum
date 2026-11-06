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

import functools
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
    VerifierContext,
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
    check: _Check, context: VerifierContext, workspace: Path, logs: Path
) -> None:
    """Archive a control or require the specific counterexample diagnostic."""
    log_path = logs / f"{check.name}.log"
    try:
        code, output = _run(check, workspace, context)
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


def main(root: Path = ROOT) -> None:
    """Execute controls and four deliberate faults without editing live sources.

    ``root`` is the repository the run is bound to. Everything the run needs —
    the workspace copy, the private run root, and the verifier pins — is
    derived from it here and passed down explicitly, so a caller can point the
    whole chain at a copy without patching a module global.
    """
    run_root = _new_boundary_run_root(root / ".cache/boundary-faults/runs")
    workspace = run_root / "workspace"
    shutil.copytree(root / "rust", workspace, ignore=shutil.ignore_patterns("target"))
    context = VerifierContext(root, read_tool_pins(root))
    # The seam takes the repository root as its first argument. Binding it here
    # keeps the run's one root authoritative; a module-level delegate would add
    # nothing but a second place to disagree about which repository this is.
    prepare = functools.partial(_prepare_boundary_cargo_home, root)
    with _boundary_cargo_run(run_root, workspace, prepare):
        logs = run_root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        _verify_progress_faults(context, workspace, logs)
        _verify_ownership_faults(context, workspace, logs)


def _verify_progress_faults(
    context: VerifierContext, workspace: Path, logs: Path
) -> None:
    """Check the production progress kernels against bound and accounting faults."""
    pins = context.pins
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
    _check(_Check("verus-control", verus, args), context, workspace, logs)
    for name, old, new in (
        ("invalid-bound", "count <= capacity", "count >= capacity"),
        ("incorrect-accounting", "total + written", "total - written"),
    ):
        proof.write_text(render(mutate(progress, old, new)), encoding="utf-8")
        _check(
            _Check(name, verus, args, "verification results::"),
            context,
            workspace,
            logs,
        )


def _verify_ownership_faults(
    context: VerifierContext, workspace: Path, logs: Path
) -> None:
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
                _Check(
                    f"{name}-control",
                    "cargo",
                    args,
                    target_dir=build_root / f"{name}-control",
                ),
                context,
                workspace,
                logs,
            )
            memory.write_text(mutate(original, old, new), encoding="utf-8")
            _check(
                _Check(name, "cargo", args, diagnostic, build_root / name),
                context,
                workspace,
                logs,
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
