#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.13"
# dependencies = ["cuprum==0.1.0"]
# ///
"""Require representative production faults to fail their designated checks.

All mutations affect a disposable workspace copy. Logs retain counterexamples
and real-unwind failures, and a successful control precedes each mutation.
"""

import dataclasses
import os
import shutil
from pathlib import Path

from cuprum import Program, ProgramCatalogue, ProjectSettings, ScopeConfig, scoped, sh
from scripts.render_boundary_proofs import render

ROOT = Path(__file__).resolve().parent.parent


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


def _run(program: str, args: tuple[str, ...], workspace: Path) -> tuple[int, str]:
    """Run one allowlisted verification tool with bounded execution time."""
    executable = Program(program)
    project = ProjectSettings(
        name="boundary-faults",
        programs=(executable,),
        documentation_locations=("docs/rust-boundary-verification.md",),
        noise_rules=(),
    )
    command = sh.make(executable, catalogue=ProgramCatalogue(projects=(project,)))
    kani = Path.home() / ".kani/kani-0.67.0"
    context = sh.ExecutionContext(
        cwd=workspace,
        env={
            "CARGO_TARGET_DIR": str(ROOT / "rust/target/boundary-faults"),
            "LD_LIBRARY_PATH": f"{kani}/toolchain/lib:{kani}/lib",
            "VERUS_Z3_PATH": str(ROOT / ".cache/boundary-z3/z3"),
            "RUSTUP_TOOLCHAIN": "1.98.0" if program != "cargo" else "1.85.0",
        },
        timeout=1200,
    )
    with scoped(ScopeConfig(allowlist=frozenset({executable}))):
        result = command(*args).run_sync(context=context)
    return result.exit_code, (result.stdout or "") + (result.stderr or "")


@dataclasses.dataclass(frozen=True, slots=True)
class _Check:
    """One verifier invocation and its expected diagnostic."""

    name: str
    program: str
    args: tuple[str, ...]
    expected_failure: str | None = None


def _check(check: _Check, workspace: Path, logs: Path) -> None:
    """Archive a control or require the specific counterexample diagnostic."""
    code, output = _run(check.program, check.args, workspace)
    (logs / f"{check.name}.log").write_text(output, encoding="utf-8")
    passed = (
        code == 0
        if check.expected_failure is None
        else code != 0 and check.expected_failure in output
    )
    if not passed:
        msg = f"unexpected fault-sensitivity result: {check.name}; inspect its log"
        raise RuntimeError(msg)


def main() -> None:
    """Execute controls and four deliberate faults without editing live sources."""
    workspace = ROOT / ".cache/boundary-faults/workspace"
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(ROOT / "rust", workspace, ignore=shutil.ignore_patterns("target"))
    logs = ROOT / "rust/target/boundary-verification/faults"
    logs.mkdir(parents=True, exist_ok=True)
    install = Path(
        os.environ.get(
            "VERUS_INSTALL_DIR",
            str(Path.home() / ".local/share/cuprum-verus-0.2026.09.06.8dea4a2"),
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
            _check(_Check(f"{name}-control", "cargo", args), workspace, logs)
            memory.write_text(mutate(original, old, new), encoding="utf-8")
            _check(_Check(name, "cargo", args, diagnostic), workspace, logs)
        finally:
            memory.write_text(original, encoding="utf-8")


if __name__ == "__main__":
    main()
