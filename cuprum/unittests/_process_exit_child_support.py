"""Children, probes, and reap helpers shared by the ``_process_exit`` cases.

The cases that must observe a *running* child hold it there rather than
starting one and hoping to be quick. A child that exits promptly leaves the
running state unobserved, so a case built on one cannot distinguish the two
states it claims to separate, and its first assertion would pass against a
zombie. :func:`_spawn_sleeping_child` keeps the child alive until the case
stops it, so the running state is held rather than raced.

The readings themselves are covered by three modules: the blocking wait and
the non-collecting observation in ``test_process_exit``, what the kernel can
still be asked about a child in ``test_process_exit_state``, and waiting for a
child to become collectable in ``test_process_exit_waitable``. They all need a
real child for the same reason: a double has no status to strand, so it
witnesses nothing about the kernel's own bookkeeping, and that is what these
helpers provide.

The blocking wait's own cases use a double for the opposite reason: the failure
they guard against — a waiter that strands after the code is already published
— cannot be asked of a real child, and a double with a published code and no
finishing wait is the only witness to it. Those two doubles live here as well,
so the module that drives them owns only the cases.

The helpers are shared across the three modules rather than owned by any one of
them, which is why the module name carries no split suffix: three of the
readings below drive a child and wait for it, and a private copy per module
would let one copy drift from the state the others assert.
"""

from __future__ import annotations

import asyncio
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] - a real child is the only witness with a status to strand.
import sys
import time

import pytest

from tests.helpers.process_state import process_state

#: How long a case waits for its child to reach the state it is observing.
_OBSERVE_TIMEOUT = 5.0

#: The status the observing cases' children choose for themselves. Non-zero and
#: not a signal's negation, so a reading that guessed could not match it.
_OBSERVED_EXIT_CODE = 7


def _spawn_exiting_child(code: int) -> subprocess.Popen[str]:
    """Start a child that ends at once with *code*.

    The child is a real process because the property under test is a property
    of the operating system's own bookkeeping: a double has no status to leave
    behind, so it can witness nothing here.

    Returns
    -------
    subprocess.Popen[str]
        The started child, deliberately not waited on: the caller reads its
        status through the probe before anything reaps it.
    """
    return subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell.
        [sys.executable, "-c", f"import sys; sys.exit({code})"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _spawn_sleeping_child() -> subprocess.Popen[str]:
    """Start a child that stays alive until the case stops it.

    The readings asserted by the cases that use this helper separate a running
    child from an exited one, so a child that exits promptly cannot witness
    them: by the time the case asks its first question the child may already be
    a zombie, and the question that was supposed to distinguish the two states
    would be answered about one. Holding the child removes that dependence on
    being quick, and the state under observation is then established by the
    case rather than hoped for.

    Returns
    -------
    subprocess.Popen[str]
        A child that sleeps until something signals or kills it. It is a real
        process for the same reason as :func:`_spawn_exiting_child`: only the
        kernel's own bookkeeping can answer what these cases ask.
    """
    # The bandit suppression for this call lives on ``_spawn_exiting_child``
    # above, where Ruff reports the rule for the module; the argv here is fixed
    # for the same reason, with no shell and no test input reaching it.
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(997)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _release_child(child: subprocess.Popen[str]) -> None:
    """Stop a held child and collect it, whatever state the case left it in.

    The child is killed through the raw handle rather than through anything the
    cases probe, so a probe under test cannot turn this cleanup into a no-op.
    The wait is what keeps the suite free of the zombie this child would
    otherwise leave behind.
    """
    if child.poll() is None:
        child.kill()
    child.wait()


def _skip_if_unobservable_exit() -> None:
    """End the case as not applicable where this host cannot see an exit.

    Two different capabilities are needed and neither can be assumed. The
    probe under test is POSIX-only, and the way the case waits for its own
    window is this host's process-state interface. Asking about *this* process
    settles the second: it is certainly running, so a record that reports the
    state as unavailable is the host saying it has no procfs to read.

    The POSIX capability is asked of the platform directly rather than read
    from ``_NON_REAPING_PROBE_AVAILABLE``. That constant is part of what these
    cases judge: read here, a probe switched off in the code under test would
    turn its own failing case into a skip, and the skip would read as a host
    limitation rather than the defect it is.

    Ends the case through *pytest.skip*, which raises rather than returns, so
    there is nothing to describe under a returns heading.
    """
    probe_platform = (
        os.name == "posix" and hasattr(os, "waitid") and hasattr(os, "CLD_EXITED")
    )
    if not probe_platform or not process_state(os.getpid()).available:
        pytest.skip("this host cannot observe an unreaped child's exit")


def _wait_until_exited(pid: int) -> None:
    """Block until *pid* has exited without being reaped.

    A ``kill(pid, 0)`` cannot answer this: the kernel keeps a zombie
    addressable until it is collected, so that probe succeeds for an exited
    child exactly as it does for a running one. Procfs reports the scheduler
    state directly, and a ``Z`` is the window these cases need — the child is
    gone, its status is still collectable, and nothing has reaped it.

    Waiting on that fact rather than on elapsed time is what keeps the cases
    honest: the assertion cannot be reached while the child is still running,
    so a failure names the probe under test rather than a race.

    Raises
    ------
    TimeoutError
        If the child has not exited within :data:`_OBSERVE_TIMEOUT`.
    """
    deadline = time.monotonic() + _OBSERVE_TIMEOUT
    while time.monotonic() < deadline:
        if process_state(pid).exited:
            return
        time.sleep(0.01)
    msg = f"child {pid} had not exited within {_OBSERVE_TIMEOUT}s"
    raise TimeoutError(msg)


class _PublishedExitProcess:
    """Process double whose waiter strands after publishing an exit code."""

    def __init__(self) -> None:
        """Start without a published return code."""
        self.returncode: int | None = None

    async def wait(self) -> int:
        """Publish success, then model asyncio's orphan pending wait future."""
        for _ in range(3):
            await asyncio.sleep(0)
        self.returncode = 0
        await asyncio.Event().wait()
        return 0


class _StrandedWaitProcess:
    """Process double whose wait task only ends through cancellation."""

    def __init__(self) -> None:
        """Start without an exit code or completed wait task."""
        self.returncode: int | None = None
        self.wait_cancelled = False
        self._waiter = asyncio.Event()

    async def wait(self) -> int:
        """Wait forever unless cleanup cancels the stranded task."""
        try:
            await self._waiter.wait()
        except asyncio.CancelledError:
            self.wait_cancelled = True
            raise
        return 0
