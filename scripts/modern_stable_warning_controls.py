"""Prove modern-stable warning denial reaches the Rust build boundaries.

The harness edits only a temporary Git worktree. It denies a cold warning,
builds that warning under Cargo's permissive policy, replays it from a warm
cache under denial, and then checks that removing it restores the route.
The temporary worktree is created from committed ``HEAD``; uncommitted source
or build-policy edits in the caller's checkout are not part of the evidence.
"""

from __future__ import annotations

import dataclasses as dc
import os
import re
import tempfile
import typing as typ
from pathlib import Path

from cuprum import ExecutionContext, Program, ProgramCatalogue, scoped, sh

ROOT = Path(__file__).resolve().parent.parent
WARNING_FUNCTION = "fn issue_506_warning_control() { std::thread::yield_now(); }"
WARNING_NAME = "issue_506_warning_control"
DENIED_WARNING_SETTING = "CARGO_BUILD_WARNINGS=deny"
WARNING_DENIAL = "error: warnings are denied by `build.warnings` configuration"
WARNING_REPLAY_STATE = "Cached warning replay"
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
GIT_PROGRAM = Program("git")
MAKE_PROGRAM = Program("make")
CATALOGUE = ProgramCatalogue.from_programs(GIT_PROGRAM, MAKE_PROGRAM)
git = sh.make(GIT_PROGRAM, catalogue=CATALOGUE)
make = sh.make(MAKE_PROGRAM, catalogue=CATALOGUE)

if typ.TYPE_CHECKING:
    from cuprum.sh import CommandResult, SafeCmdBuilder


@dc.dataclass(frozen=True, slots=True)
class Gate:
    """One migrated Make target and the Rust package receiving a warning."""

    name: str
    target: str
    package: str
    source: Path


@dc.dataclass(frozen=True, slots=True)
class GateRun:
    """One captured target invocation and its Cargo freshness observations."""

    gate: str
    package: str
    phase: str
    exit_code: int
    output: str
    package_state: str
    compile_lines: int
    fresh_lines: int


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


def _observe_output(
    gate: Gate,
    phase: str,
    exit_code: int,
    output: str,
) -> GateRun:
    """Summarize Cargo freshness output for one warning-control invocation."""
    # Cargo may colour its `Checking` and `Fresh` prefixes even when captured.
    # Strip terminal escapes for classification while preserving the raw log.
    lines = [ANSI_ESCAPE.sub("", line).lstrip() for line in output.splitlines()]
    package_state = next(
        (
            line.split(maxsplit=1)[0]
            for line in lines
            if line.startswith(("Compiling ", "Checking ", "Fresh "))
            and f"{gate.package} v" in line
        ),
        "not-reported",
    )
    if _is_cached_warning_replay(package_state, output):
        # Cargo replays the cached warning without printing a Fresh line for
        # the local package that owns that warning.
        package_state = WARNING_REPLAY_STATE
    return GateRun(
        gate=gate.name,
        package=gate.package,
        phase=phase,
        exit_code=exit_code,
        output=output,
        package_state=package_state,
        compile_lines=sum(
            line.startswith(("Compiling ", "Checking ")) for line in lines
        ),
        fresh_lines=sum(line.startswith("Fresh ") for line in lines),
    )


def _is_cached_warning_replay(package_state: str, output: str) -> bool:
    """Recognize Cargo's warning-only cached replay diagnostic."""
    if package_state != "not-reported":
        return False
    if WARNING_NAME not in output:
        return False
    return WARNING_DENIAL in output


def _child_environment() -> dict[str, str]:
    """Return the bounded subprocess environment, clearing inherited Make flags."""
    environment = os.environ.copy()
    # OVERLAY re-reads os.environ, so a deleted MAKEFLAGS would be re-inherited.
    environment.update(
        MAKEFLAGS="", CARGO_BUILD_JOBS="2", CARGO_TERM_COLOR="never", TEST_JOBS="2"
    )
    return environment


def _run(
    program: SafeCmdBuilder, arguments: tuple[str, ...], cwd: Path
) -> tuple[int, str]:
    """Run one allowlisted command with bounded Cargo and test concurrency."""
    context = ExecutionContext(cwd=cwd.as_posix(), env=_child_environment())
    with scoped(catalogue=CATALOGUE):
        result: CommandResult = program(*arguments).run_sync(context=context)
    output = (result.stdout or "") + (result.stderr or "")
    return result.exit_code, output


def _run_gate(worktree: Path, gate: Gate, phase: str) -> GateRun:
    """Run one modern-stable Make target and retain its full log."""
    exit_code, output = _run(make, ("-C", worktree.as_posix(), gate.target), ROOT)
    observed = _observe_output(gate, phase, exit_code, output)
    log_directory = ROOT / "rust/target/modern-stable-warning-controls"
    log_directory.mkdir(parents=True, exist_ok=True)
    (log_directory / f"{gate.name}-{phase}.log").write_text(output, encoding="utf-8")
    return observed


def _has_warning_failure(run: GateRun) -> bool:
    """Require Cargo's denial error and the injected warning marker."""
    return (
        run.exit_code != 0
        and WARNING_NAME in run.output
        and WARNING_DENIAL in run.output
    )


def _has_cached_warning_failure(run: GateRun) -> bool:
    """Require denial to replay a warning without rebuilding its package."""
    has_cached_state = run.package_state in {"Fresh", WARNING_REPLAY_STATE}
    return (
        _has_warning_failure(run)
        and has_cached_state
        and not _package_was_rebuilt(run)
        and run.fresh_lines > 0
    )


def _package_was_rebuilt(run: GateRun) -> bool:
    """Require Cargo to compile or check the selected local crate."""
    return run.package_state in {"Compiling", "Checking"}


def _warn_policy_prewarm(
    worktree: Path, gate: Gate, policies: WarningPolicies
) -> GateRun:
    """Build the injected warning once under the permissive policy."""
    policies.select("warn")
    try:
        return _run_gate(worktree, gate, "warning-prewarm")
    finally:
        policies.restore()


def _verify_cold(run: GateRun, failures: list[str]) -> None:
    """Record a cold control that did not compile and reject the warning."""
    if not _has_warning_failure(run) or not _package_was_rebuilt(run):
        failures.append(f"{run.gate} cold run did not compile and fail on the warning")


def _verify_prewarm(run: GateRun, failures: list[str]) -> None:
    """Require the permissive run to replay or compile the local warning."""
    is_warning = run.exit_code == 0 and WARNING_NAME in run.output
    has_package_state = run.package_state in {"Fresh", "Compiling", "Checking"}
    if not is_warning or not has_package_state:
        failures.append(f"{run.gate} warn-policy prewarm did not cache the warning")


def _verify_warm(run: GateRun, failures: list[str]) -> None:
    """Require denial to replay a warning from the cached local package."""
    if not _has_cached_warning_failure(run):
        failures.append(
            f"{run.gate} {run.phase} did not replay the warning "
            "from a fresh local package"
        )


def _exercise_gate(
    worktree: Path, gate: Gate, policies: WarningPolicies
) -> tuple[tuple[GateRun, ...], tuple[str, ...]]:
    """Run cold denial, permissive prewarm, warm denial, then clean source."""
    source_path = worktree / gate.source
    original = source_path.read_text(encoding="utf-8")
    failures: list[str] = []
    results: list[GateRun] = []
    source_path.write_text(_append_warning(original), encoding="utf-8")
    try:
        cold = _run_gate(worktree, gate, "warning-cold")
        results.append(cold)
        _verify_cold(cold, failures)

        prewarm = _warn_policy_prewarm(worktree, gate, policies)
        results.append(prewarm)
        _verify_prewarm(prewarm, failures)

        for phase in ("warning-warm", "warning-warm-repeat"):
            warm = _run_gate(worktree, gate, phase)
            results.append(warm)
            _verify_warm(warm, failures)
    finally:
        policies.restore()
        source_path.write_text(original, encoding="utf-8")

    clean = _run_gate(worktree, gate, "clean")
    results.append(clean)
    if clean.exit_code != 0:
        failures.append(f"{gate.name} clean-source rerun failed ({clean.exit_code})")
    elif not _package_was_rebuilt(clean):
        failures.append(f"{gate.name} clean-source rerun did not rebuild its package")
    return tuple(results), tuple(failures)


def _exercise_gates(worktree: Path) -> tuple[tuple[GateRun, ...], tuple[str, ...]]:
    """Exercise all migrated gates against one dedicated target directory."""
    makefile_path = worktree / "Makefile"
    denied = makefile_path.read_text(encoding="utf-8")
    policies = WarningPolicies(
        path=makefile_path,
        denied=denied,
        permissive=_with_warning_policy(denied, "warn"),
    )
    results: list[GateRun] = []
    failures: list[str] = []
    for gate in GATES:
        gate_results, gate_failures = _exercise_gate(worktree, gate, policies)
        results.extend(gate_results)
        failures.extend(gate_failures)
    return tuple(results), tuple(failures)


def _add_worktree(path: Path) -> str | None:
    """Create a detached worktree at the current candidate commit."""
    code, output = _run(
        git,
        ("worktree", "add", "--detach", path.as_posix(), "HEAD"),
        ROOT,
    )
    return None if code == 0 else f"git worktree add failed ({code}): {output}"


def _remove_worktree(path: Path) -> str | None:
    """Remove only the worktree created by this control run."""
    code, output = _run(
        git,
        ("worktree", "remove", "--force", path.as_posix()),
        ROOT,
    )
    return None if code == 0 else f"git worktree remove failed ({code}): {output}"


def _run_controls() -> tuple[tuple[GateRun, ...], tuple[str, ...]]:
    """Run all controls in a temporary worktree with one shared target."""
    with tempfile.TemporaryDirectory(
        prefix=".modern-stable-warning-controls-", dir=ROOT.parent
    ) as temporary_directory:
        worktree = Path(temporary_directory) / "worktree"
        setup_failure = _add_worktree(worktree)
        if setup_failure is not None:
            return (), (setup_failure,)
        try:
            results, failures = _exercise_gates(worktree)
        finally:
            cleanup_failure = _remove_worktree(worktree)
        if cleanup_failure is not None:
            failures = (*failures, cleanup_failure)
    return results, failures


def _report(results: tuple[GateRun, ...], failures: tuple[str, ...]) -> str:
    """Render bounded control results for the terminal and retained report."""
    rows = [
        (
            "| gate | package | phase | exit | package state | compile/check "
            "lines | fresh lines |"
        ),
        "| --- | --- | ---: | ---: | --- | ---: | ---: |",
    ]
    rows.extend(
        f"| {run.gate} | {run.package} | {run.phase} | {run.exit_code} | "
        f"{run.package_state} | {run.compile_lines} | {run.fresh_lines} |"
        for run in results
    )
    rows.extend(("", "Failures:", ""))
    if failures:
        rows.extend(f"- {failure}" for failure in failures)
    else:
        rows.append("- none")
    return "\n".join(rows) + "\n"


def main() -> int:
    """Run warning controls and write the report to the Rust target directory.

    Parameters
    ----------
    None

    Returns
    -------
    int
        Zero when every control has the expected result, otherwise one.
    """
    results, failures = _run_controls()
    report = _report(results, failures)
    report_path = ROOT / "rust/target/modern-stable-warning-controls/report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    print(report, end="")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
