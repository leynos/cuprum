"""Interpret the Cargo output produced by the modern-stable warning controls.

The control harness runs Make targets in a temporary worktree and reads Cargo's
freshness lines back. This module turns that output into a :class:`GateRun`,
decides whether each phase met its expectation, and renders the report. It
performs no process or file-system work, which keeps the decision rules usable
without a compiler.
"""

from __future__ import annotations

import dataclasses as dc
import re
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

WARNING_NAME = "issue_506_warning_control"
WARNING_DENIAL = "error: warnings are denied by `build.warnings` configuration"
WARNING_REPLAY_STATE = "Cached warning replay"
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


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


def observe_output(
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


def has_warning_failure(run: GateRun) -> bool:
    """Require Cargo's denial error and the injected warning marker."""
    return (
        run.exit_code != 0
        and WARNING_NAME in run.output
        and WARNING_DENIAL in run.output
    )


def package_was_rebuilt(run: GateRun) -> bool:
    """Require Cargo to compile or check the selected local crate."""
    return run.package_state in {"Compiling", "Checking"}


def _has_cached_warning_failure(run: GateRun) -> bool:
    """Require denial to replay a warning without rebuilding its package."""
    has_cached_state = run.package_state in {"Fresh", WARNING_REPLAY_STATE}
    return (
        has_warning_failure(run)
        and has_cached_state
        and not package_was_rebuilt(run)
        and run.fresh_lines > 0
    )


def verify_cold(run: GateRun, failures: list[str]) -> None:
    """Record a cold control that did not compile and reject the warning."""
    if not has_warning_failure(run) or not package_was_rebuilt(run):
        failures.append(f"{run.gate} cold run did not compile and fail on the warning")


def verify_prewarm(run: GateRun, failures: list[str]) -> None:
    """Require the permissive run to replay or compile the local warning."""
    is_warning = run.exit_code == 0 and WARNING_NAME in run.output
    has_package_state = run.package_state in {"Fresh", "Compiling", "Checking"}
    if not is_warning or not has_package_state:
        failures.append(f"{run.gate} warn-policy prewarm did not cache the warning")


def verify_warm(run: GateRun, failures: list[str]) -> None:
    """Require denial to replay a warning from the cached local package."""
    if not _has_cached_warning_failure(run):
        failures.append(
            f"{run.gate} {run.phase} did not replay the warning "
            "from a fresh local package"
        )


def render_report(results: tuple[GateRun, ...], failures: tuple[str, ...]) -> str:
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
