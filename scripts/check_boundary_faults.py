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

import dataclasses
import os
import shutil
from pathlib import Path

from cuprum import (
    ProcessGroupPolicy,
    Program,
    ProgramCatalogue,
    ProjectSettings,
    ScopeConfig,
    scoped,
    sh,
)
from scripts.boundary_cargo import (
    boundary_cargo_run as _boundary_cargo_run,
)
from scripts.boundary_cargo import (
    new_run_root as _new_boundary_run_root,
)
from scripts.boundary_cargo import offline_cargo_environment
from scripts.boundary_cargo import (
    prepare_run_cargo_home as _prepare_boundary_cargo_home,
)
from scripts.cargo_state_metadata import _sha256
from scripts.render_boundary_proofs import render

ROOT = Path(__file__).resolve().parent.parent


@dataclasses.dataclass(frozen=True, slots=True)
class ToolPins:
    """Pinned verifier versions, read from the repository's tool manifests."""

    kani: str
    verus: str


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


class _LockfileRewriteError(RuntimeError):
    """Report a verifier that rewrote its lockfile, retaining its output."""

    def __init__(self, message: str, output: str) -> None:
        """Carry the verifier's diagnostic stream with the rewrite failure."""
        super().__init__(message)
        self.output = output


def read_tool_pins() -> ToolPins:
    """Read the pinned Kani and Verus versions from the repository.

    The manifests are read when a verification path runs, not at import time,
    so importing :func:`mutate` does not require either pin file to exist.

    A missing, unreadable, or empty pin is a hard failure rather than a default:
    a blank version would otherwise build a verifier path that does not exist
    and report the resulting launch failure as a failed proof.

    Returns
    -------
    ToolPins
        The versions pinned by ``tools/kani/VERSION`` and
        ``tools/verus/VERSION``.

    Raises
    ------
    OSError
        If a pin file cannot be read.
    ValueError
        If a pin file exists but records no version.
    """  # ruff: ignore[docstring-extraneous-exception] - OSError/ValueError propagate from _read_pin.
    return ToolPins(
        kani=_read_pin("kani"),
        verus=_read_pin("verus"),
    )


def _read_pin(tool: str) -> str:
    """Return one tool's pinned version, refusing a file that records none."""
    pin = (ROOT / f"tools/{tool}/VERSION").read_text(encoding="utf-8").strip()
    if not pin:
        msg = f"tools/{tool}/VERSION records no version"
        raise ValueError(msg)
    return pin


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


@dataclasses.dataclass(frozen=True, slots=True)
class _Invocation:
    """One verifier command and the lockfile contract that must survive it."""

    command_arguments: tuple[str, ...]
    environment: dict[str, str]
    lockfile: Path | None = None
    lockfile_digest: str | None = None


def _invocation(
    program: str,
    args: tuple[str, ...],
    workspace: Path,
    pins: ToolPins,
    target_dir: Path | None = None,
) -> _Invocation:
    """Build the verifier environment and its arguments, freezing native Cargo.

    Cargo consumes a global ``--frozen`` before dispatching an external
    subcommand, and cargo-kani 0.67.0 rejects that flag itself, so a Kani
    invocation cannot receive it. The offline environment is the supported
    mechanism there; the copied lockfile is compared afterwards instead.

    ``target_dir`` overrides the shared build directory. Kani decides whether
    to rebuild from that directory alone, so a directory reused across a
    mutation and its control can serve the mutation's prebuilt harness to the
    control, which then reports the fault it was meant to precede.

    Returns
    -------
    _Invocation
        The arguments, environment, and lockfile contract for this program.
    """
    kani = Path.home() / f".kani/kani-{pins.kani}"
    target_directory = (
        target_dir
        or workspace.parent / "target/boundary-faults-debug-all-features"
    )
    environment = os.environ.copy()
    environment.update({
        "CARGO_TARGET_DIR": str(target_directory),
        "LD_LIBRARY_PATH": f"{kani}/toolchain/lib:{kani}/lib",
        "VERUS_Z3_PATH": str(ROOT / ".cache/boundary-z3/z3"),
        "RUSTUP_TOOLCHAIN": "1.98.0" if program != "cargo" else "1.87.0",
    })
    if program != "cargo":
        return _Invocation(args, environment)
    environment = offline_cargo_environment(
        workspace,
        target_directory,
        caller_environment=environment,
    )
    if args[0] != "kani":
        return _Invocation(("--frozen", *args), environment)
    lockfile = workspace / "Cargo.lock"
    return _Invocation(args, environment, lockfile, _sha256(lockfile))


def _run(
    program: str,
    args: tuple[str, ...],
    workspace: Path,
    target_dir: Path | None = None,
) -> tuple[int, str]:
    """Run one allowlisted verification tool with bounded execution time.

    ``target_dir`` gives a check its own build directory. Kani decides whether
    to rebuild from that directory alone, so a directory reused across a
    mutation and its control can serve the mutation's prebuilt harness to the
    control. The control then reports the fault it was meant to precede.

    Returns
    -------
    tuple[int, str]
        The verifier's exit code and its combined standard output and error.
    """
    pins = read_tool_pins()
    executable = Program(program)
    project = ProjectSettings(
        name="boundary-faults",
        programs=(executable,),
        documentation_locations=("docs/rust-boundary-verification.md",),
        noise_rules=(),
    )
    command = sh.make(executable, catalogue=ProgramCatalogue(projects=(project,)))
    invocation = _invocation(program, args, workspace, pins, target_dir)
    context = sh.ExecutionContext(
        cwd=workspace,
        env=invocation.environment,
        timeout=1200,
        process_group=ProcessGroupPolicy.OWN_GROUP,
    )
    with scoped(ScopeConfig(allowlist=frozenset({executable}))):
        result = command(*invocation.command_arguments).run_sync(context=context)
    output = (result.stdout or "") + (result.stderr or "")
    if (
        invocation.lockfile is not None
        and _sha256(invocation.lockfile) != invocation.lockfile_digest
    ):
        msg = f"cargo kani changed the copied lockfile: {invocation.lockfile}"
        raise _LockfileRewriteError(msg, output)
    return result.exit_code, output


@dataclasses.dataclass(frozen=True, slots=True)
class _Check:
    """One verifier invocation and its expected diagnostic."""

    name: str
    program: str
    args: tuple[str, ...]
    expected_failure: str | None = None


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
    # They live beside the run's own workspace, under the archived
    # `.cache/boundary-faults/runs` tree, rather than under `logs`. CI uploads
    # `rust/target/boundary-verification` whole with `if: always()`, so a build
    # tree in the log directory would ship a full Cargo target directory as an
    # artefact on every run.
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
            memory.write_text(original, encoding="utf-8")


if __name__ == "__main__":
    main()
# weave: run 'weave explain scripts/check_boundary_faults.py' for per-hunk detail, 'weave check' to verify your resolution
