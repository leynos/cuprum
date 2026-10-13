"""Whether the kernel can still be asked about a child, and what that buys.

:func:`~cuprum._process_exit._child_was_collected` reports whether the kernel
can still be asked about a child at all, so its cases drive real children
through the states a delivery decision has to tell apart. A double cannot
witness them: it has no status to strand, so the states these cases separate
are ones only the kernel's own bookkeeping holds.

The cases that must observe a *running* child hold it there rather than
starting one and hoping to be quick. A child that exits promptly leaves the
running state unobserved, so a case built on one cannot distinguish the two
states it claims to separate, and its first assertion would pass against a
zombie. :func:`~cuprum.unittests._process_exit_child_support._spawn_sleeping_child`
keeps the child alive until the case stops it, so the running state is held
rather than raced.

The blocking wait asks a neighbouring question and lives in
:mod:`cuprum.unittests.test_process_exit_waitable`; the blocking wait itself
and the non-collecting observation are in
:mod:`cuprum.unittests.test_process_exit`.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys

import pytest

from cuprum import _wait4_process
from cuprum._process_exit import _child_was_collected
from cuprum.unittests._process_exit_child_support import (
    _OBSERVED_EXIT_CODE,
    _release_child,
    _skip_if_unobservable_exit,
    _spawn_exiting_child,
    _spawn_sleeping_child,
    _wait_until_exited,
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
