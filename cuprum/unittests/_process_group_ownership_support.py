"""Shared scaffolding for the opt-in process-group ownership test modules.

The ownership suite is split by concern — teardown behaviour, policy
configuration, and the spawn a policy implies — but all three drive the same
runs and need the same platform gate, spawn helpers, and recorded-pid
assertions. They live here so the split modules share one definition rather
than drifting apart.

Liveness is taken from the grandchild itself, which records its own pid before
it blocks. Asserting against a recorded pid rather than a group identifier
keeps every signal the tests send inside the group the run created: a group
signal aimed at anything the test runner belongs to would take pytest with it.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import os
import signal
import sys
import time
import typing as typ

import pytest

from cuprum._process_group import (
    _POST_KILL_SETTLEMENT_S,
    _GroupProbe,
    _probe_group,
)
from cuprum._process_lifecycle import _terminate_all_shielded
from cuprum._teardown_policy import _TeardownPolicy
from tests.helpers.process_exit import ExitObservation, observe_exit
from tests.helpers.timeouts import (
    pipe_holding_child_argv,
    python_interpreter,
    wait_for_pid_file,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

# The ownership guarantee is a POSIX process-group guarantee. Windows has no
# equivalent primitive, and the policy is rejected there rather than emulated,
# so the tests that exercise it are scoped to the platform that has it.
_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups are unavailable on Windows",
)


@dc.dataclass(frozen=True, slots=True)
class _OwnedRun:
    """One run under test, with the descendants it started."""

    process: asyncio.subprocess.Process
    grandchild_pid: int
    unrelated_pid: int


async def _spawn_owned_run(tmp_path: Path) -> _OwnedRun:
    """Start a run whose child holds a pipe through a SIGTERM-immune grandchild.

    The child is spawned as its own session leader, exactly as ``OWN_GROUP``
    spawns it in production, so the group the tests tear down is the group the
    run created rather than one the test runner belongs to.

    Returns
    -------
    _OwnedRun
        The running child, its grandchild's pid, and the pid of an unrelated
        process started alongside it.
    """
    pid_file = tmp_path / "grandchild.pid"
    unrelated = await asyncio.create_subprocess_exec(
        python_interpreter(),
        "-c",
        "import time; time.sleep(300)",
    )
    process = await asyncio.create_subprocess_exec(
        python_interpreter(),
        *pipe_holding_child_argv(pid_file),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    # The pid file is written after the handler is installed, so its arrival
    # is the readiness signal as well as the source of the pid to assert on.
    grandchild_pid = await asyncio.to_thread(
        wait_for_pid_file,
        pid_file,
        context="grandchild readiness",
    )
    return _OwnedRun(process, grandchild_pid, unrelated.pid)


async def _spawn_immune_child(ready: Path) -> asyncio.subprocess.Process:
    """Start a child that ignores ``SIGTERM``, once it announces it is armed.

    Spawned as its own session leader, so the group a teardown signals is one
    this process created rather than the one the test runner belongs to.

    The child writes ``ready`` only after installing its handler, so waiting on
    that file is waiting for a genuinely immune child. Both halves matter: a
    cooperative child would exit on the first signal and settle the run before
    the escalation was ever reached, and an immune child signalled during
    interpreter start-up would die on the default disposition instead of the
    one this needs it to take.

    Returns
    -------
    asyncio.subprocess.Process
        The running child, armed and ready to be signalled.
    """
    process = await asyncio.create_subprocess_exec(
        python_interpreter(),
        "-c",
        "import os, pathlib, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        "time.sleep(300)",
        str(ready),
        start_new_session=True,
    )
    await asyncio.to_thread(
        wait_for_pid_file,
        ready,
        context="immune child readiness",
    )
    return process


def _observed_exit(pid: int) -> ExitObservation:
    """Return *pid*'s observation, failing where the host cannot report it."""
    observed = observe_exit(pid)
    if observed is ExitObservation.UNOBSERVABLE:
        pytest.fail(
            f"this host cannot report whether process {pid} has exited, so "
            "the descendant's fate is unproven"
        )
    return observed


def _descendant_is_running(pid: int) -> bool:
    """Report whether *pid* is running, never counting an unreaped zombie."""
    return _observed_exit(pid) is ExitObservation.RUNNING


def _descendant_has_exited(pid: int) -> bool:
    """Report whether *pid* has exited, counting an unreaped zombie as exited."""
    return _observed_exit(pid) is ExitObservation.EXITED


def _cleanup_may_signal_group(pgid: int, descendant_pid: int) -> bool:
    """Report whether cleanup may signal *pgid* without risking a stranger.

    A running descendant keeps the group identifier alive, so the observation
    is the anchor. Where the host cannot report the descendant, fall back to
    the same group probe production teardown uses: only a group that still
    reports a signalable member is signalled. An empty or unsignalable group is
    left alone, because its identifier may already have been reused.

    Returns
    -------
    bool
        Whether the group still holds a member this run can safely reach.
    """
    observed = observe_exit(descendant_pid)
    if observed is ExitObservation.RUNNING:
        return True
    if observed is ExitObservation.UNOBSERVABLE:
        return _probe_group(pgid) is _GroupProbe.MEMBERS
    return False


async def _cleanup_owned_run(
    run: _OwnedRun,
    *,
    task: asyncio.Task[None] | None = None,
) -> None:
    """Best-effort cleanup for an owned run, safe inside a ``finally`` block.

    The optional *task* is a teardown still in flight; it is cancelled and
    given the post-kill settlement window to stop. Neither that wait nor the
    kills can raise a test failure, so cleanup never replaces the failure that
    entered the block.
    """
    if task is not None and not task.done():
        task.cancel()
        await asyncio.wait({task}, timeout=_POST_KILL_SETTLEMENT_S)
    pgid = run.process.pid
    if pgid is not None and _cleanup_may_signal_group(pgid, run.grandchild_pid):
        await _kill_and_wait(pgid, run.grandchild_pid, group=True)
    await _kill_and_wait(run.unrelated_pid, run.unrelated_pid, group=False)


def _await_descendant_exit(pid: int, *, seconds: float) -> bool:
    """Wait up to *seconds* for *pid* to exit, zombies counting as exited.

    This never fails. Cleanup paths use it, where an unobservable host or a
    process that outlives the bound must not replace the failure that entered
    the cleanup.

    Returns
    -------
    bool
        Whether the exit was observed.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        observed = observe_exit(pid)
        if observed is ExitObservation.EXITED:
            return True
        if observed is ExitObservation.UNOBSERVABLE:
            return False
        time.sleep(0.05)
    return False


def _wait_for_descendant_exit(
    pid: int,
    *,
    seconds: float = 5.0,
    context: str = "descendant teardown",
) -> None:
    """Fail unless *pid* has exited within *seconds*, zombies counting as exited.

    Darwin refuses a group signal whose only remaining member is an unreaped
    zombie and treats that group as settled, so a null-signal probe can report
    a killed descendant as running until the host's reaper collects it.
    ``observe_exit`` reads the scheduler state instead, which is the answer the
    teardown promised.
    """
    if not _await_descendant_exit(pid, seconds=seconds):
        pytest.fail(f"Process {pid} did not exit within {seconds}s ({context})")


async def _kill_and_wait(
    kill_pid: int,
    wait_pid: int,
    *,
    group: bool,
) -> None:
    """Best-effort cleanup: kill *kill_pid*, then wait for *wait_pid*.

    Neither half fails. The wait is bounded and returns a verdict rather than a
    test failure, so an unobservable host or a process that outlives the bound
    cannot replace the failure that entered the ``finally`` block.
    """
    _kill_ignoring_missing(kill_pid, group=group)
    await asyncio.to_thread(_await_descendant_exit, wait_pid, seconds=5.0)


async def _terminate_owned_run(run: _OwnedRun) -> None:
    """Tear the run down through the ordinary teardown entry."""
    await _terminate_all_shielded(
        (run.process,),
        _TeardownPolicy(0.5, owns_group=True),
    )
    await asyncio.to_thread(
        _wait_for_descendant_exit,
        run.grandchild_pid,
        context="owned-group teardown",
    )
    await run.process.wait()


def _kill_ignoring_missing(pid: int, *, group: bool) -> None:
    """Force-kill *pid* — or its group — tolerating a target already gone."""
    try:
        if group:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
