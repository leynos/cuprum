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
import tempfile
from pathlib import Path

__all__ = ["ROOT", "ScenarioResult", "run_scenario"]

#: The repository root, two levels above ``tests/helpers``.
ROOT = Path(__file__).resolve().parents[2]

#: The scenario module the supervisor runs in the child interpreter.
_SCENARIO_MODULE = "tests.helpers.live_byte_scenarios"
#: The directory a scenario may use to publish a process group for reclaim.
_SCENARIO_DIR_ENV = "CUPRUM_LIVE_SCENARIO_DIR"


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


def run_scenario(
    name: str,
    *,
    timeout: float = 20.0,
    env: dict[str, str] | None = None,
) -> ScenarioResult:
    """Run scenario *name* in its own session under a hard deadline.

    Parameters
    ----------
    name : str
        The scenario name the child module dispatches.
    timeout : float, default=20.0
        Seconds the scenario may run before its session is killed.
    env : dict[str, str] | None, default=None
        Extra environment entries for the scenario process.

    Returns
    -------
    ScenarioResult
        The exit code, captured streams, and whether the deadline fired.
    """
    command = [sys.executable, "-m", _SCENARIO_MODULE, name]
    session = {"start_new_session": True} if os.name == "posix" else {}
    with tempfile.TemporaryDirectory(prefix="cuprum-live-supervisor-") as marker_dir:
        child_env = dict(os.environ)
        child_env[_SCENARIO_DIR_ENV] = marker_dir
        if env is not None:
            child_env.update(env)
        with subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=child_env,
            **session,
        ) as process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_session(process)
                _reclaim_owned_group(Path(marker_dir))
                stdout, stderr = process.communicate()
                return ScenarioResult(
                    name, process.returncode or -1, stdout, stderr, timed_out=True
                )
            return ScenarioResult(
                name, process.returncode, stdout, stderr, timed_out=False
            )


def _kill_session(process: subprocess.Popen[str]) -> None:
    """Kill the scenario's whole session where the platform provides one."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        return
    process.kill()


def _reclaim_owned_group(marker_dir: Path) -> None:
    """Kill a process group the scenario created outside its own session.

    A scenario that exercises ``OWN_GROUP`` makes its child the leader of a new
    session, which the supervisor's session kill cannot reach. The scenario
    publishes that group's identifier in the marker directory before it waits,
    so a rescued timeout does not leave the group running.
    """
    marker = marker_dir / "owned-group.pid"
    if os.name != "posix" or not marker.is_file():
        return
    text = marker.read_text(encoding="utf-8").strip()
    if not text.isdigit():
        return
    try:
        os.killpg(int(text), signal.SIGKILL)
    except ProcessLookupError:
        return
