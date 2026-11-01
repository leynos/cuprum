"""Shared fixtures for the modern-stable warning-control tests.

The control harness is tested at two levels: its orchestration contracts and
the command entry point. Both need the same prepared worktree and the same
expected answer for each control phase, so those live here rather than being
copied into either test module.
"""

from __future__ import annotations

import typing as typ

from scripts.modern_stable_warning_controls import GATES, WARNING_FUNCTION
from scripts.modern_stable_warning_observation import (
    WARNING_DENIAL,
    WARNING_REPLAY_STATE,
    GateRun,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

#: A Makefile whose modern route denies warnings through Cargo's setting.
POLICY_MAKEFILE = (
    "MODERN_STABLE_ENV = CARGO_BUILD_WARNINGS=deny "
    "CARGO_TARGET_DIR=rust/target/modern-stable\n"
)
#: Source text every gate source a control run edits starts from.
FIXTURE_SOURCE = "//! Fixture module.\n"
#: Phases each configured gate runs, in order.
CONTROL_PHASES = (
    "warning-cold",
    "warning-prewarm",
    "warning-warm",
    "warning-warm-repeat",
    "clean",
)


def write_worktree(worktree: pth.Path) -> None:
    """Write the Makefile and every gate source a control run can edit."""
    for gate in GATES:
        source = worktree / gate.source
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(FIXTURE_SOURCE, encoding="utf-8")
    (worktree / "Makefile").write_text(POLICY_MAKEFILE, encoding="utf-8")


def control_run(gate: str, phase: str) -> GateRun:
    """Return the result each phase of a healthy control run produces."""
    if phase == "warning-cold":
        return GateRun(
            gate,
            "cuprum-streams",
            phase,
            101,
            f"warning: {WARNING_FUNCTION}\n{WARNING_DENIAL}",
            "Checking",
            1,
            0,
        )
    if phase == "warning-prewarm":
        return GateRun(
            gate, "cuprum-streams", phase, 0, WARNING_FUNCTION, "Fresh", 0, 1
        )
    if phase == "clean":
        return GateRun(
            gate,
            "cuprum-streams",
            phase,
            0,
            "Compiling cuprum-streams",
            "Compiling",
            1,
            0,
        )
    return GateRun(
        gate,
        "cuprum-streams",
        phase,
        101,
        f"Fresh dep\nwarning: {WARNING_FUNCTION}\n{WARNING_DENIAL}",
        WARNING_REPLAY_STATE,
        0,
        1,
    )
