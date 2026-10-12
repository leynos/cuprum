"""Regression coverage for resilient subprocess exit waiting.

Four readings are covered here, and they answer different questions.
:func:`~cuprum._process_exit._await_process_exit` blocks until a child is gone,
and the cases for it use a double because the failure it guards against — a
waiter that strands after the code is already published — cannot be asked of a
real child. :func:`~cuprum._process_exit._observed_exit_code` only *looks*, and
its cases use a real child, because what it must not do is collect the status
it observes. A double cannot witness that: it has no status to strand.

The last two ask about a child's state rather than its outcome, and neither
can be answered by a double either.
:func:`~cuprum._process_exit._child_was_collected` reports whether the kernel
can still be asked about a child at all, so its cases drive real children
through the states a delivery decision has to tell apart.
:func:`~cuprum._process_exit._await_child_waitable` waits for one of those
states to arrive without consuming what it waits for, which a double has no
status to strand and so cannot witness.

The cases that must observe a *running* child hold it there rather than
starting one and hoping to be quick. A child that exits promptly leaves the
running state unobserved, so a case built on one cannot distinguish the two
states it claims to separate, and its first assertion would pass against a
zombie. :func:`_spawn_sleeping_child` keeps the child alive until the case
stops it, so the running state is held rather than raced.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import] - a real child is the only witness with a status to strand.
import sys
import time
import typing as typ

import pytest

from cuprum import _wait4_process
from cuprum._process_exit import (
    _PROCESS_EXIT_INITIAL_POLL_INTERVAL,
    _PROCESS_EXIT_MAX_POLL_INTERVAL,
    _await_child_waitable,
    _await_process_exit,
    _child_was_collected,
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


def _spawn_sleeping_child() -> subprocess.Popen[str]:
    """Start a child that stays alive until the case stops it.

    The readings asserted here separate a running child from an exited one, so
    a child that exits promptly cannot witness them: by the time the case asks
    its first question the child may already be a zombie, and the question that
    was supposed to distinguish the two states would be answered about one.
    Holding the child removes that dependence on being quick, and the state
    under observation is then established by the case rather than hoped for.

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


def _as_process(child: subprocess.Popen[str]) -> asyncio.subprocess.Process:
    """Present a real child to a probe that declares a process it may not modify.

    The probes take ``asyncio.subprocess.Process`` and read only ``pid`` and
    ``returncode`` from it, but ``subprocess.Popen`` is the only way to start
    a child whose waiting the case itself controls. The cast records that
    narrow use rather than widening the probe's declared type to accommodate
    a test.

    Returns
    -------
    asyncio.subprocess.Process
        The same child, presented under the type the probes declare. No
        attribute is copied or adapted, so a probe reading it reaches the real
        process.
    """
    return typ.cast("asyncio.subprocess.Process", child)


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


def test_child_was_collected_is_false_for_a_child_still_waitable() -> None:
    """A child this process can still wait for must not read as collected.

    The states that answer *false* are the ones a delivery may still reach.
    They are not interchangeable, so the case walks both: a child that is
    still running holds its PID because it is alive, and a zombie holds it
    because the kernel will not release a PID until its status is collected.
    A check that treated either as "gone" would refuse deliveries that were
    perfectly deliverable, which is a regression in the other direction from
    the one this guard exists to prevent.

    The child is held alive for the first question. A child that exited
    promptly would answer it while already a zombie, so the case could not
    distinguish the two states it exists to separate — the running answer
    would be established by nothing, and a guard that reported every child as
    collected would still pass. The exit is then produced by the case, so the
    zombie state is one the case created rather than one it waited for.
    """
    _skip_if_unobservable_exit()
    running = _spawn_sleeping_child()
    try:
        assert not _child_was_collected(running.pid), (
            "a running child is still this process's to wait for"
        )
        running.kill()
        _wait_until_exited(running.pid)
        assert not _child_was_collected(running.pid), (
            "an exited but uncollected child is still waitable, and still "
            "holds its PID against reuse"
        )
    finally:
        _release_child(running)


def test_child_was_collected_is_true_once_the_status_is_gone() -> None:
    """A child whose status is collected must read as beyond any delivery.

    This is the state the guard exists to detect, and it is the one a
    ``returncode`` check cannot see from the backend's own thread: the reap
    that collected the status may not have published anything yet. Once the
    status is gone the kernel is free to reuse the PID, so a signal sent by
    number could reach an unrelated process — which is why the answer here
    must be ``true`` even though nothing has been signalled.

    A foreign ``waitpid`` performs the collection, so the case does not
    depend on the reaper under test having run, only on the kernel state that
    a completed collection leaves behind.
    """
    _skip_if_unobservable_exit()
    child = _spawn_exiting_child(_OBSERVED_EXIT_CODE)
    reaped_pid, status = os.waitpid(child.pid, 0)
    assert reaped_pid == child.pid, "the foreign reaper must collect this child"
    assert os.waitstatus_to_exitcode(status) == _OBSERVED_EXIT_CODE, (
        "the foreign reaper must have taken the real status, or the case "
        "would be reading a state it did not create"
    )
    assert _child_was_collected(child.pid), (
        "a collected child must not read as waitable, or a delivery would "
        "signal a PID the kernel may already have reused"
    )


def test_child_was_collected_leaves_a_zombie_for_its_reaper() -> None:
    """Asking the guard's question must not consume the status it asks about.

    The question is asked on the delivery path, and a delivery is refused
    precisely when the run is still settling. If asking collected the status
    as a side effect, the refusal itself would strand the reap: the owner
    would find ``ChildProcessError`` where the exit code and resource usage
    should have been. ``WNOWAIT`` is what prevents that, and this case is the
    one that would notice it being dropped — the guard would answer correctly
    while the run settled with no outcome at all.
    """
    _skip_if_unobservable_exit()
    child = _spawn_exiting_child(_OBSERVED_EXIT_CODE)
    try:
        _wait_until_exited(child.pid)
        assert not _child_was_collected(child.pid), (
            "the child must still be waitable when the guard asks"
        )
        reaped = child.wait()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    assert reaped == _OBSERVED_EXIT_CODE, (
        "asking the guard's question must leave the status for the reaper, "
        f"got {reaped}"
    )


def test_child_was_collected_answers_the_same_for_a_foreign_pid() -> None:
    """Pin the kernel's one answer for both "collected" and "never ours".

    ``waitid`` raises ``ChildProcessError`` when no waitable child of this
    process exists at a PID, and that single errno covers two situations it
    does not distinguish: a child this process collected, and a PID that was
    never its child at all. This case pins that, because the guard's honesty
    depends on it rather than being a surprise a later reader rediscovers.

    The looseness is safe *because of where the guard is asked*. Its callers
    pass a PID this owner spawned, so the second situation does not arise, and
    if it somehow did, the answer errs towards refusing a delivery rather than
    towards signalling a PID the kernel may have handed to a stranger.
    """
    _skip_if_unobservable_exit()
    assert _child_was_collected(os.getpid()), (
        "this process's own PID is not its own child; the kernel reports that "
        "with the same error it uses for a collected child, so the guard must "
        "not be read as proving a collection happened"
    )


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="asserts a POSIX child's signal delivery",
)
def test_send_signal_refuses_from_the_guard_before_any_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal must come from the guard, not from ``os.kill`` failing.

    This is the window the published ``returncode`` cannot see. A foreign
    reaper has collected the child, so ``returncode`` still reads ``None``
    while the kernel no longer has a waitable child at that PID — the state a
    delivery encounters when a reap has collected a status but not yet
    published it. A guard consulting only ``returncode`` would pass the check
    and call ``os.kill`` on a PID the kernel is free to have recycled.

    The exception alone does not prove which path refused it, because
    ``os.kill`` also raises ``ProcessLookupError`` for a collected child. So
    ``os.kill`` is replaced with a recorder: the delivery is refused either
    way, but only the guard's refusal leaves the recorder untouched, and the
    old code fails this case on that assertion rather than on the exception.
    """
    killed: list[tuple[int, int]] = []

    def record_kill(pid: int, signal_number: int) -> None:
        """Record an attempted kill without delivering anything."""
        killed.append((pid, signal_number))

    async def spawn_child() -> _wait4_process._Wait4Process:
        """Spawn the child whose status a foreign reaper will collect."""
        config = _wait4_process.DirectProcessConfig(
            argv=(sys.executable, "-c", f"raise SystemExit({_OBSERVED_EXIT_CODE})"),
            stdin=None,
            stdout=None,
            stderr=None,
            env=None,
            cwd=None,
        )
        process = await _wait4_process.spawn_wait4_process(config)
        assert isinstance(process, _wait4_process._Wait4Process), (
            "the POSIX path must yield the wait4-owning process"
        )
        return process

    monkeypatch.setattr(_wait4_process.os, "kill", record_kill)
    # The foreign collection and the delivery are synchronous, so they run
    # outside the event loop rather than blocking it.
    process = asyncio.run(spawn_child())
    reaped_pid, _status = os.waitpid(process.pid, 0)
    assert reaped_pid == process.pid, "the foreign reaper must collect it"
    try:
        process.send_signal(signal.SIGTERM)
    except ProcessLookupError as exc:
        refusal: ProcessLookupError | None = exc
    else:
        refusal = None

    assert refusal is not None, (
        "a child whose status is collected must be refused; ``returncode`` "
        "reads None here, so only the kernel question can refuse it"
    )
    assert not killed, (
        "the guard must refuse before any kill is attempted, or a PID the "
        f"kernel may have reused is signalled by number; kill was called "
        f"{len(killed)} time(s)"
    )


def test_await_child_waitable_blocks_then_leaves_the_status() -> None:
    """Waiting for a child must last until it is collectable, and not collect.

    This is what keeps the reaper's locked section to a few instructions: the
    lock is entered only once ``wait4`` has nothing left to wait for, so no
    delivery is ever parked behind a child's lifetime. Two properties have to
    hold together for that to be true, and each would be broken by the obvious
    wrong implementation of the other.

    The wait must actually wait — a probe that answered at once would report a
    running child as collectable, and the collection that followed would block
    inside the locked section, which is the defect the ordering exists to
    avoid. And it must not collect: a probe that consumed the status would
    strand it, and the reaper would find nothing where the child's exit code
    and resource usage should be.

    The child is held alive while the first property is asserted, which is
    what separates a genuine wait from a probe that merely answers: a child
    that had already exited would make both implementations agree. It is then
    killed by the case, so the wait is proven to end on a state the case
    itself produced.
    """
    _skip_if_unobservable_exit()
    child = _spawn_sleeping_child()
    try:
        assert _observed_exit_code(_as_process(child)) is None, (
            "the child must be running when the wait begins, or the case "
            "cannot show the wait was what brought it to collectable"
        )
        child.kill()
        assert _await_child_waitable(child.pid), (
            "a child that has exited must be reported collectable"
        )
        # The wait has returned, and the status must still be there for the
        # reaper that owns it.
        reaped = child.wait()
    finally:
        _release_child(child)
    assert reaped == -signal.SIGKILL, (
        f"waiting for the child must leave its status for the reaper, got {reaped}"
    )


def test_await_child_waitable_reports_a_child_that_is_already_gone() -> None:
    """A child whose status is collected must not be reported collectable.

    The wait is the reaper's route into the locked section, so its answer has
    to be right in the direction that matters most. Reporting ``True`` for a
    child that is gone would carry the reaper into a collection that cannot
    succeed, replacing a recorded status with an invariant failure — and the
    delivery side of the same question would then be asked against a
    half-settled instance.

    A foreign reaper collects the status here, which is the state a completed
    collection leaves behind whatever performed it. The answer must be
    ``False``, and the caller that owns the reap is then free to fall back to
    its own collection and report the mismatch rather than proceeding as if
    the child were ready.
    """
    _skip_if_unobservable_exit()
    child = _spawn_exiting_child(_OBSERVED_EXIT_CODE)
    reaped_pid, status = os.waitpid(child.pid, 0)
    assert reaped_pid == child.pid, "the foreign reaper must collect this child"
    assert os.waitstatus_to_exitcode(status) == _OBSERVED_EXIT_CODE, (
        "the foreign reaper must have taken the real status"
    )
    assert not _await_child_waitable(child.pid), (
        "a child whose status is collected must not be reported collectable"
    )
