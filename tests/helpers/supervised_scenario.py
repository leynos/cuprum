"""Run one live-byte scenario in a separate, externally bounded interpreter.

A live stream test can hang in ways a unit test cannot observe: a consumer
stalled against backpressure, a drain waiting on a pipe a descendant holds
open, or a settlement that never reaps. Bounding such a scenario with
``pytest-timeout`` alone would abandon the child processes it started, because
the timeout cancels the test but does not own the session those processes
formed.

The supervisor here runs each scenario as its own process in a new session, so
a timeout can kill the whole session rather than the direct child. On POSIX
that is ``killpg``; on Windows it falls back to killing the direct child and
leaves a descendant that inherited a pipe to be reaped by the operating
system, which is the platform's documented gap. The scenario reports success
by exiting zero, so a rescued timeout is a failure rather than a pass.

The helper deliberately returns its result instead of asserting: it lives
outside the test-only trees, and the calling ``test_*.py`` module owns the
assertions and their diagnostics.
"""

from __future__ import annotations

import dataclasses as dc
import os
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import] - runs the repository's own scenario module.
import sys
from pathlib import Path

__all__ = ["ROOT", "ScenarioResult", "run_scenario"]

#: The repository root, two levels above ``tests/helpers``.
ROOT = Path(__file__).resolve().parents[2]

#: The scenario module the supervisor runs in the child interpreter.
_SCENARIO_MODULE = "tests.helpers.live_byte_scenarios"


@dc.dataclass(frozen=True, slots=True)
class ScenarioResult:
    """One supervised scenario's exit and captured output."""

    name: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def ok(self) -> bool:
        """Whether the scenario exited zero before its deadline.

        Returns
        -------
        bool
            ``True`` only for a completed, successful run.
        """
        return not self.timed_out and self.returncode == 0


def run_scenario(name: str, *, timeout: float = 20.0) -> ScenarioResult:
    """Run scenario *name* in its own session under a hard deadline.

    Parameters
    ----------
    name : str
        The scenario name the child module dispatches.
    timeout : float, default=20.0
        Seconds the scenario may run before its session is killed.

    Returns
    -------
    ScenarioResult
        The exit code, captured streams, and whether the deadline fired.
    """
    command = [sys.executable, "-m", _SCENARIO_MODULE, name]
    session = {"start_new_session": True} if os.name == "posix" else {}
    with subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **session,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_session(process)
            stdout, stderr = process.communicate()
            return ScenarioResult(
                name, process.returncode or -1, stdout, stderr, timed_out=True
            )
        return ScenarioResult(name, process.returncode, stdout, stderr, timed_out=False)


def _kill_session(process: subprocess.Popen[str]) -> None:
    """Kill the scenario's whole session where the platform provides one."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        return
    process.kill()
