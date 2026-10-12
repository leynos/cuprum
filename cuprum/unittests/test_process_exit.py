"""Regression coverage for resilient subprocess exit waiting.

Two readings are covered here, and they answer different questions.
:func:`~cuprum._process_exit._await_process_exit` blocks until a child is gone,
and the cases for it use a double because the failure it guards against — a
waiter that strands after the code is already published — cannot be asked of a
real child. :func:`~cuprum._process_exit._observed_exit_code` only *looks*, and
its cases use a real child, because what it must not do is collect the status
it observes. A double cannot witness that: it has no status to strand.
"""

from __future__ import annotations

import asyncio
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] - a real child is the only witness with a status to strand.
import sys
import time
import typing as typ

import pytest

from cuprum._process_exit import (
    _PROCESS_EXIT_INITIAL_POLL_INTERVAL,
    _PROCESS_EXIT_MAX_POLL_INTERVAL,
    _await_process_exit,
    _observed_exit_code,
)
from tests.helpers.process_state import process_state

#: How long a case waits for its child to reach the state it is observing.
_OBSERVE_TIMEOUT = 5.0

#: The status the observing cases' children choose for themselves. Non-zero and
#: not a signal's negation, so a reading that guessed could not match it.
_OBSERVED_EXIT_CODE = 7


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


def test_await_process_exit_accepts_published_returncode() -> None:
    """A published return code wins over an orphan pending waiter."""

    async def run_case() -> int:
        """Wait for the stranded process double without hanging."""
        process = typ.cast("asyncio.subprocess.Process", _PublishedExitProcess())
        return await asyncio.wait_for(_await_process_exit(process), timeout=0.5)

    assert asyncio.run(run_case()) == 0, (
        "a published exit code must complete a stranded process waiter"
    )


def test_await_process_exit_backs_off_while_waiter_is_stranded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lost-wakeup fallback backs off and cancels its losing waiter."""

    async def run_case() -> tuple[int, list[float], bool]:
        """Publish an exit code after enough polls to reach the backoff cap."""
        process = _StrandedWaitProcess()
        intervals: list[float] = []
        original_sleep = asyncio.sleep

        async def record_sleep(interval: float) -> None:
            """Record each delay before eventually publishing the exit code."""
            intervals.append(interval)
            if len(intervals) == 10:
                process.returncode = 0
            await original_sleep(0)

        monkeypatch.setattr("cuprum._process_exit.sleep", record_sleep)
        exit_code = await _await_process_exit(
            typ.cast("asyncio.subprocess.Process", process)
        )
        return exit_code, intervals, process.wait_cancelled

    exit_code, intervals, wait_cancelled = asyncio.run(run_case())

    assert exit_code == 0, "the published exit code must complete the fallback"
    assert intervals == [
        min(
            _PROCESS_EXIT_INITIAL_POLL_INTERVAL * 2**index,
            _PROCESS_EXIT_MAX_POLL_INTERVAL,
        )
        for index in range(10)
    ], "long-lived waiters must use capped exponential backoff"
    assert wait_cancelled, "completion must cancel the stranded process waiter"


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


def test_observed_exit_code_reads_a_child_no_wait_has_reaped() -> None:
    """A merely-held child's status is observable without collecting it.

    This is the reading a terminal refusal depends on. A handle that holds a
    child no wait has reaped sees ``returncode`` as ``None`` however long ago
    the child exited, so a refusal that consulted only that attribute would
    report the placeholder ``-1`` — or, worse, find nothing to refuse and
    deliver a signal into a child that is already gone.
    """
    _skip_if_unobservable_exit()
    child = _spawn_exiting_child(_OBSERVED_EXIT_CODE)
    try:
        _wait_until_exited(child.pid)
        # The case is only meaningful while the child is unreaped: were it
        # collected, ``returncode`` would carry the status and a reading that
        # consulted it would be indistinguishable from one that did not. This
        # is also what keeps the case honest against a later ``_wait_until_exited``
        # that merely times out on a running child.
        assert child.returncode is None, (
            "the child must still be unreaped when the probe reads it"
        )
        observed = _observed_exit_code(typ.cast("asyncio.subprocess.Process", child))
    finally:
        child.wait()
    assert observed == _OBSERVED_EXIT_CODE, (
        f"an exited unreaped child must report its own status "
        f"{_OBSERVED_EXIT_CODE}, got {observed}"
    )


def test_observed_exit_code_leaves_the_status_for_the_reaper() -> None:
    """The probe must not consume the status its owner still has to collect.

    Reading a child's exit is only half the job: the reading is worthless if it
    collects the status as a side effect, because the run's own reaper then
    finds nothing to record and the handle settles with no exit code at all.
    ``WNOWAIT`` is what makes the difference, and this is the case that would
    notice if it were dropped — the observed value would be right while the
    collected one turned into ``ChildProcessError``.
    """
    _skip_if_unobservable_exit()
    child = _spawn_exiting_child(_OBSERVED_EXIT_CODE)
    try:
        _wait_until_exited(child.pid)
        assert child.returncode is None, (
            "the child must still be unreaped when the probe reads it"
        )
        observed = _observed_exit_code(typ.cast("asyncio.subprocess.Process", child))
        reaped = child.wait()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    assert observed == _OBSERVED_EXIT_CODE, "the probe must read the child's status"
    assert reaped == _OBSERVED_EXIT_CODE, (
        "observing an exit must leave the status for the reaper to collect, "
        f"got {reaped}"
    )
