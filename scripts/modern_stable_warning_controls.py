"""Prove modern-stable warning denial reaches the Rust build boundaries.

The harness edits only a temporary Git worktree. It denies a cold warning,
builds that warning under Cargo's permissive policy, replays it from a warm
cache under denial, and then checks that removing it restores the route.
The temporary worktree is created from committed ``HEAD``; uncommitted source
or build-policy edits in the caller's checkout are not part of the evidence.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import os
import tempfile
import typing as typ
from pathlib import Path

from cuprum import (
    ExecutionContext,
    Program,
    ProgramCatalogue,
    TimeoutExpired,
    scoped,
    sh,
)
from scripts.modern_stable_warning_observation import (
    Gate,
    GateRun,
    observe_output,
    package_was_rebuilt,
    render_report,
    verify_cold,
    verify_prewarm,
    verify_warm,
)

ROOT = Path(__file__).resolve().parent.parent
WARNING_FUNCTION = "fn issue_506_warning_control() { std::thread::yield_now(); }"
DENIED_WARNING_SETTING = "CARGO_BUILD_WARNINGS=deny"
#: A cold gate builds the whole workspace under compile admission, so this bound
#: is generous; it exists to name a wedged Make or Cargo, not to pace a build.
GATE_TIMEOUT_SECONDS = 3600
#: Exit status reported for a gate that outlived :data:`GATE_TIMEOUT_SECONDS`.
GATE_TIMEOUT_EXIT_CODE = 124
GIT_PROGRAM = Program("git")
MAKE_PROGRAM = Program("make")
CATALOGUE = ProgramCatalogue.from_programs(GIT_PROGRAM, MAKE_PROGRAM)
git = sh.make(GIT_PROGRAM, catalogue=CATALOGUE)
make = sh.make(MAKE_PROGRAM, catalogue=CATALOGUE)

if typ.TYPE_CHECKING:
    from cuprum.sh import CommandResult, SafeCmdBuilder

#: Runs one Make target in a worktree and reports what Cargo printed.
type GateRunner = cabc.Callable[[Path, Gate, str], GateRun]
#: Creates or removes the isolated worktree, returning a failure message.
type WorktreeStep = cabc.Callable[[Path, Path], str | None]


@dc.dataclass(frozen=True, slots=True)
class WarningPolicies:
    """Hold the exact denied and permissive Makefiles used by the controls."""

    path: Path
    denied: str
    permissive: str

    def select(self, policy: str) -> None:
        """Write the already-validated policy variant to the worktree."""
        selected = self.denied if policy == "deny" else self.permissive
        self.path.write_text(selected, encoding="utf-8")

    def restore(self) -> None:
        """Restore the route's checked-in warning denial."""
        self.select("deny")


GATES = (
    Gate(
        "check",
        "modern-stable-cargo-check",
        "cuprum-streams",
        Path("rust/cuprum-streams/src/lib.rs"),
    ),
    Gate(
        "clippy",
        "modern-stable-clippy",
        "cuprum-streams",
        Path("rust/cuprum-streams/src/lib.rs"),
    ),
    Gate(
        "test",
        "modern-stable-test",
        "cuprum-streams",
        Path("rust/cuprum-streams/src/lib.rs"),
    ),
    Gate(
        "maturin",
        "modern-stable-develop",
        "cuprum-rust",
        Path("rust/cuprum-rust/src/lib.rs"),
    ),
)


def _append_warning(source: str) -> str:
    """Append one deliberately unused private function to a Rust module."""
    if WARNING_FUNCTION in source:
        msg = "the modern-stable warning marker already exists in the source"
        raise ValueError(msg)
    separator = "" if source.endswith("\n") else "\n"
    return f"{source}{separator}\n{WARNING_FUNCTION}\n"


def _with_warning_policy(makefile: str, policy: str) -> str:
    """Return the Makefile with only the modern route's policy changed."""
    if policy not in {"warn", "deny"}:
        msg = f"unsupported Cargo warning policy: {policy}"
        raise ValueError(msg)
    if makefile.count(DENIED_WARNING_SETTING) != 1:
        msg = "the Makefile must set modern Cargo warning denial exactly once"
        raise ValueError(msg)
    return makefile.replace(DENIED_WARNING_SETTING, f"CARGO_BUILD_WARNINGS={policy}")


def _child_environment() -> dict[str, str]:
    """Return the bounded subprocess environment, clearing inherited Make flags."""
    environment = os.environ.copy()
    # OVERLAY re-reads os.environ, so a deleted MAKEFLAGS would be re-inherited.
    environment.update(
        MAKEFLAGS="", CARGO_BUILD_JOBS="2", CARGO_TERM_COLOR="never", TEST_JOBS="2"
    )
    return environment


def _timeout_output(arguments: tuple[str, ...], error: TimeoutExpired) -> str:
    """Describe an expired gate, retaining whatever the command printed."""
    captured = "".join(
        part.decode(errors="replace") if isinstance(part, bytes) else part
        for part in (error.output, error.stderr)
        if part
    )
    message = f"gate timed out after {GATE_TIMEOUT_SECONDS}s: {' '.join(arguments)}"
    return f"{captured}\n{message}\n"


def _run(
    program: SafeCmdBuilder, arguments: tuple[str, ...], cwd: Path
) -> tuple[int, str]:
    """Run one allowlisted command with bounded Cargo and test concurrency."""
    context = ExecutionContext(
        cwd=cwd.as_posix(), env=_child_environment(), timeout=GATE_TIMEOUT_SECONDS
    )
    try:
        with scoped(catalogue=CATALOGUE):
            result: CommandResult = program(*arguments).run_sync(context=context)
    except TimeoutExpired as error:
        # Expiry is a gate failure, not a crash: the caller still logs and reports.
        return GATE_TIMEOUT_EXIT_CODE, _timeout_output(arguments, error)
    output = (result.stdout or "") + (result.stderr or "")
    return result.exit_code, output


def _run_gate(worktree: Path, gate: Gate, phase: str, root: Path = ROOT) -> GateRun:
    """Run one modern-stable Make target and retain its full log."""
    exit_code, output = _run(make, ("-C", worktree.as_posix(), gate.target), root)
    observed = observe_output(gate, phase, exit_code, output)
    log_directory = root / "rust/target/modern-stable-warning-controls"
    log_directory.mkdir(parents=True, exist_ok=True)
    (log_directory / f"{gate.name}-{phase}.log").write_text(output, encoding="utf-8")
    return observed


def _warn_policy_prewarm(
    worktree: Path,
    gate: Gate,
    policies: WarningPolicies,
    run_gate: GateRunner,
) -> GateRun:
    """Build the injected warning once under the permissive policy."""
    policies.select("warn")
    try:
        return run_gate(worktree, gate, "warning-prewarm")
    finally:
        policies.restore()


def _exercise_gate(
    worktree: Path,
    gate: Gate,
    policies: WarningPolicies,
    run_gate: GateRunner,
) -> tuple[tuple[GateRun, ...], tuple[str, ...]]:
    """Run cold denial, permissive prewarm, warm denial, then clean source."""
    source_path = worktree / gate.source
    original = source_path.read_text(encoding="utf-8")
    failures: list[str] = []
    results: list[GateRun] = []
    source_path.write_text(_append_warning(original), encoding="utf-8")
    try:
        cold = run_gate(worktree, gate, "warning-cold")
        results.append(cold)
        verify_cold(cold, failures)

        prewarm = _warn_policy_prewarm(worktree, gate, policies, run_gate)
        results.append(prewarm)
        verify_prewarm(prewarm, failures)

        for phase in ("warning-warm", "warning-warm-repeat"):
            warm = run_gate(worktree, gate, phase)
            results.append(warm)
            verify_warm(warm, failures)
    finally:
        policies.restore()
        source_path.write_text(original, encoding="utf-8")

    clean = run_gate(worktree, gate, "clean")
    results.append(clean)
    if clean.exit_code != 0:
        failures.append(f"{gate.name} clean-source rerun failed ({clean.exit_code})")
    elif not package_was_rebuilt(clean):
        failures.append(f"{gate.name} clean-source rerun did not rebuild its package")
    return tuple(results), tuple(failures)


def _exercise_gates(
    worktree: Path, gates: tuple[Gate, ...], run_gate: GateRunner
) -> tuple[tuple[GateRun, ...], tuple[str, ...]]:
    """Exercise every configured gate against one dedicated target directory."""
    makefile_path = worktree / "Makefile"
    denied = makefile_path.read_text(encoding="utf-8")
    policies = WarningPolicies(
        path=makefile_path,
        denied=denied,
        permissive=_with_warning_policy(denied, "warn"),
    )
    results: list[GateRun] = []
    failures: list[str] = []
    for gate in gates:
        gate_results, gate_failures = _exercise_gate(worktree, gate, policies, run_gate)
        results.extend(gate_results)
        failures.extend(gate_failures)
    return tuple(results), tuple(failures)


def _add_worktree(path: Path, root: Path = ROOT) -> str | None:
    """Create a detached worktree at the current candidate commit."""
    code, output = _run(
        git,
        ("worktree", "add", "--detach", path.as_posix(), "HEAD"),
        root,
    )
    return None if code == 0 else f"git worktree add failed ({code}): {output}"


def _remove_worktree(path: Path, root: Path = ROOT) -> str | None:
    """Remove only the worktree created by this control run."""
    code, output = _run(
        git,
        ("worktree", "remove", "--force", path.as_posix()),
        root,
    )
    return None if code == 0 else f"git worktree remove failed ({code}): {output}"


@dc.dataclass(frozen=True, slots=True)
class ControlHarness:
    """Injected boundaries that create, exercise, and clean one worktree.

    Attributes
    ----------
    gates : tuple[Gate, ...]
        Gates to exercise, in the order their results are reported.
    add_worktree : WorktreeStep
        Creates the isolated worktree from the candidate commit.
    remove_worktree : WorktreeStep
        Removes the worktree this run created.
    run_gate : cabc.Callable[[Path, Gate, str, Path], GateRun]
        Runs one Make target in the worktree and retains its log under ``root``.

    Notes
    -----
    The file-system and process boundaries are injectable so a caller can
    exercise the orchestration without a real Cargo build, while the default
    harness performs the real run.
    """

    gates: tuple[Gate, ...] = GATES
    add_worktree: WorktreeStep = _add_worktree
    remove_worktree: WorktreeStep = _remove_worktree
    run_gate: cabc.Callable[[Path, Gate, str, Path], GateRun] = _run_gate


DEFAULT_HARNESS = ControlHarness()


def _run_controls(
    root: Path = ROOT, harness: ControlHarness = DEFAULT_HARNESS
) -> tuple[tuple[GateRun, ...], tuple[str, ...]]:
    """Run all controls in a temporary worktree with one shared target."""
    with tempfile.TemporaryDirectory(
        prefix=".modern-stable-warning-controls-", dir=root.parent
    ) as temporary_directory:
        worktree = Path(temporary_directory) / "worktree"
        setup_failure = harness.add_worktree(worktree, root)
        if setup_failure is not None:
            return (), (setup_failure,)

        def run_gate(worktree_path: Path, gate: Gate, phase: str) -> GateRun:
            """Bind the run gate to the root that owns the retained logs."""
            return harness.run_gate(worktree_path, gate, phase, root)

        try:
            results, failures = _exercise_gates(worktree, harness.gates, run_gate)
        finally:
            cleanup_failure = harness.remove_worktree(worktree, root)
        if cleanup_failure is not None:
            failures = (*failures, cleanup_failure)
    return results, failures


def main(root: Path = ROOT, harness: ControlHarness = DEFAULT_HARNESS) -> int:
    """Run warning controls and write the report to the Rust target directory.

    Parameters
    ----------
    root : pathlib.Path
        Repository whose committed ``HEAD`` is exercised; defaults to this
        checkout.
    harness : ControlHarness
        Injected boundaries for the run; the default performs real work.

    Returns
    -------
    int
        Zero when every control has the expected result, otherwise one.
    """
    results, failures = _run_controls(root=root, harness=harness)
    report = render_report(results, failures)
    report_path = root / "rust/target/modern-stable-warning-controls/report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    print(report, end="")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
