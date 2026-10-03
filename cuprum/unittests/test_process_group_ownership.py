"""Opt-in process-group ownership contains a child's descendants on POSIX.

A run spawned with ``ProcessGroupPolicy.OWN_GROUP`` owns the process group its
child leads, so teardown can signal the whole group rather than the direct
child alone. What these tests assert is the difference that ownership makes: a
grandchild that inherits the run's pipe and ignores ``SIGTERM`` is gone once
the run is torn down, the pipe is released, and a process outside the group is
untouched.

Liveness is taken from the grandchild itself, which records its own pid before
it blocks. Asserting against a recorded pid rather than a group identifier
keeps every signal this module sends inside the group the run created: a group
signal aimed at anything the test runner belongs to would take pytest with it.

The default policy is checked here too. ``INHERIT`` must keep signalling the
direct child alone, so these tests would fail if ownership were ever the
default rather than an opt-in.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import logging
import os
import signal
import sys
import time
import typing as typ

import pytest

from cuprum import ECHO, _process_signal, _subprocess_context, _wait4_process, sh
from cuprum._process_group import _POST_KILL_SETTLEMENT_S
from cuprum._process_lifecycle import _terminate_all_shielded
from cuprum._teardown_policy import _TeardownPolicy
from cuprum.sh import ExecutionContext, ProcessGroupPolicy, RunOutputOptions
from tests.helpers.timeouts import (
    pipe_holding_child_argv,
    process_is_running,
    python_interpreter,
    wait_for_pid_file,
    wait_for_process_death,
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

pytestmark = _posix_only


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
    marker = tmp_path / "grandchild.ready"
    unrelated = await asyncio.create_subprocess_exec(
        python_interpreter(),
        "-c",
        "import time; time.sleep(300)",
    )
    process = await asyncio.create_subprocess_exec(
        python_interpreter(),
        *pipe_holding_child_argv(pid_file, marker),
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


async def _terminate_owned_run(run: _OwnedRun) -> None:
    """Tear the run down through the ordinary teardown entry."""
    await _terminate_all_shielded((run.process,), _TeardownPolicy(0.5, owns_group=True))
    await asyncio.to_thread(
        wait_for_process_death,
        run.grandchild_pid,
        context="owned-group teardown",
    )
    await run.process.wait()


def test_owned_group_teardown_terminates_a_grandchild_that_ignores_sigterm(
    tmp_path: Path,
) -> None:
    """Owning the group is what reaches a descendant the direct child left behind.

    The grandchild ignores ``SIGTERM`` and is not the process the run holds a
    handle to, so a teardown that signals only the direct child leaves it
    running. Owning the group signals it too, and the SIGKILL escalation ends
    it despite its handler.
    """

    async def run_case() -> None:
        """Tear down an owned group while its grandchild is still up."""
        run = await _spawn_owned_run(tmp_path)
        assert process_is_running(run.grandchild_pid), (
            "the grandchild must be running before teardown, or this test "
            "cannot observe what the teardown does"
        )
        await _terminate_owned_run(run)
        assert not process_is_running(run.grandchild_pid), (
            "the grandchild must be gone once the owned group is torn down"
        )

    asyncio.run(run_case())


def test_owned_group_teardown_releases_the_inherited_pipe(tmp_path: Path) -> None:
    """The drain reaches EOF because no live process still owns the write end.

    A reader waiting for end-of-file on the run's stdout cannot settle while
    the grandchild holds that descriptor. Releasing the pipe is the observable
    consequence of containing the descendant, and it is what the bounded
    capture grace window otherwise papers over.
    """

    async def run_case() -> None:
        """Drain stdout after a teardown that released the write end."""
        run = await _spawn_owned_run(tmp_path)
        assert run.process.stdout is not None, "the run must expose its stdout pipe"
        await _terminate_owned_run(run)
        remaining = await asyncio.wait_for(run.process.stdout.read(), timeout=5.0)
        assert remaining == b"", (
            "stdout must reach EOF once the grandchild holding it is gone"
        )

    asyncio.run(run_case())


def test_owned_group_teardown_leaves_an_unrelated_process_untouched(
    tmp_path: Path,
) -> None:
    """Ownership is bounded to the group the run created.

    The unrelated process is spawned without ``start_new_session``, so it
    belongs to a different group. If teardown signalled anything wider than the
    run's own group, that process — and the test runner — would die with it.
    """

    async def run_case() -> None:
        """Tear down one run and leave a neighbouring process alone."""
        run = await _spawn_owned_run(tmp_path)
        try:
            await _terminate_owned_run(run)
            assert process_is_running(run.unrelated_pid), (
                "a process outside the owned group must survive its teardown"
            )
        finally:
            os.kill(run.unrelated_pid, signal.SIGKILL)
            await asyncio.to_thread(
                wait_for_process_death,
                run.unrelated_pid,
                context="unrelated process cleanup",
            )

    asyncio.run(run_case())


def test_inherited_policy_signals_only_the_direct_child(tmp_path: Path) -> None:
    """The default policy keeps the direct-child teardown callers have today.

    This is the control for the tests above: the same run, torn down without
    ownership, leaves the grandchild alive. Without this, a bug that contained
    descendants unconditionally would pass every other test in this module.
    """

    async def run_case() -> None:
        """Tear down without ownership, as the default policy does."""
        run = await _spawn_owned_run(tmp_path)
        try:
            await _terminate_all_shielded(
                (run.process,),
                _TeardownPolicy(0.5, owns_group=False),
            )
            await run.process.wait()
            assert process_is_running(run.grandchild_pid), (
                "an unowned teardown must not reach the grandchild"
            )
        finally:
            os.killpg(run.process.pid, signal.SIGKILL)
            await asyncio.to_thread(
                wait_for_process_death,
                run.grandchild_pid,
                context="grandchild cleanup",
            )
            os.kill(run.unrelated_pid, signal.SIGKILL)
            await asyncio.to_thread(
                wait_for_process_death,
                run.unrelated_pid,
                context="unrelated process cleanup",
            )

    asyncio.run(run_case())


def test_owned_group_teardown_of_an_exited_child_is_not_an_error(
    tmp_path: Path,
) -> None:
    """Tearing down a group whose leader has already exited raises nothing.

    Every member of the group is gone by the time this runs, so ``os.killpg``
    finds no one to signal. The teardown must absorb that the way it absorbs a
    signal to an already-reaped direct child, rather than turning a completed
    run's cleanup into a failure.
    """

    async def run_case() -> None:
        """Tear down a group whose leader has already been reaped."""
        process = await asyncio.create_subprocess_exec(
            python_interpreter(),
            "-c",
            "pass",
            start_new_session=True,
        )
        await process.wait()
        # The leader is reaped; the call must still return quietly.
        await _terminate_all_shielded((process,), _TeardownPolicy(0.5, owns_group=True))
        assert process.returncode is not None, (
            "the exited child must keep its recorded exit code"
        )

    asyncio.run(run_case())


def test_repeated_cancellation_does_not_abandon_owned_group_cleanup(
    tmp_path: Path,
) -> None:
    """A cancelled teardown still completes before the cancellation propagates.

    Teardown is wrapped in the shielded primitive precisely so a caller's
    cancellation cannot abandon it part-way and leave an owned group running.
    Cancelling twice pins the escalated case: the first cancellation is
    absorbed while the grace period is still counting down.
    """

    async def run_case() -> None:
        """Cancel a teardown twice and confirm it still completed."""
        run = await _spawn_owned_run(tmp_path)
        try:
            task = asyncio.create_task(_terminate_owned_run(run))
            # Let the teardown reach its grace-period wait before interrupting
            # it, so the cancellation lands inside the shielded region we mean
            # to exercise rather than before it starts.
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not process_is_running(run.grandchild_pid), (
                "cleanup must finish even when the caller cancels twice"
            )
        finally:
            os.kill(run.unrelated_pid, signal.SIGKILL)
            await asyncio.to_thread(
                wait_for_process_death,
                run.unrelated_pid,
                context="unrelated process cleanup",
            )

    asyncio.run(run_case())


def test_an_unreapable_group_member_does_not_stall_the_escalation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The post-``SIGKILL`` group wait is bounded, and reports what it left.

    A group member whose parent died first can be re-parented outside this
    run, so its exit is recorded only when something else reaps it. Waiting
    for that without a bound would let a teardown outlive its grace period
    indefinitely, so the wait is capped and the outstanding members reported
    instead. The bound is deliberately not derived from ``cancel_grace``: that
    bounds how long a member is *asked* to leave, not how long the run waits
    after compelling it.

    The direct child is still awaited without a bound, and that is the
    important half. A run must reap the process it spawned even when the group
    around it cannot settle, or the caller is left with a zombie it owns.

    The child ignores ``SIGTERM`` so the grace period genuinely times out and
    the escalation is reached, and it announces itself only once its handler
    is installed. Both halves matter: a cooperative child would exit on the
    first signal and settle the run before the bound ever applied, and an
    immune child signalled during interpreter start-up would die on the
    default disposition instead of the one this test needs it to take.
    """

    async def refusing_group_wait(pgid: int) -> None:
        """Stand in for a group that never reports itself empty.

        The parameter matches the real probe's single argument. A double that
        asked for more would raise on the call rather than be waited on, and
        the teardown gathers its targets with ``return_exceptions=True``, so
        the mismatch would be swallowed and the test would pass without the
        bound ever engaging.
        """
        del pgid
        await asyncio.sleep(3600)

    # Patched where it is looked up rather than where it is defined: the
    # teardown calls the name it imported, so patching the defining module
    # would leave the real poll loop running and the test would pass on it.
    monkeypatch.setattr(_process_signal, "_await_group_exit", refusing_group_wait)

    async def run_case() -> None:
        """Escalate against a group that will never settle."""
        ready = tmp_path / "immune.ready"
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
        # Signalling before this point would reach the interpreter's default
        # disposition rather than the installed handler, and the child would
        # die on SIGTERM — taking the escalation path with it.
        await asyncio.to_thread(
            wait_for_pid_file,
            ready,
            context="immune child readiness",
        )
        # The grace period has to be short enough to take the escalation
        # promptly but non-zero: a zero-length wait is a corner case that can
        # complete without ever suspending, which would settle the run on the
        # first phase and skip the bounded wait this test exists to pin.
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger=_process_signal.__name__):
            await _terminate_all_shielded(
                (process,),
                _TeardownPolicy(0.1, owns_group=True),
            )
        elapsed = time.monotonic() - started

        assert process.returncode is not None, (
            "the direct child must be reaped even when its group cannot settle"
        )
        assert elapsed < _POST_KILL_SETTLEMENT_S * 3, (
            "the group wait must be bounded rather than waiting for a member "
            f"this run cannot reap; took {elapsed:.3f}s"
        )

    asyncio.run(run_case())

    assert any(
        "process_group_settlement_timeout" in record.getMessage()
        for record in caplog.records
    ), (
        "members left outstanding must be reported rather than passed over "
        f"silently, got {caplog.messages}"
    )


def test_owned_policy_is_rejected_where_posix_groups_do_not_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OWN_GROUP`` refuses rather than silently containing nothing.

    The refusal is raised while building the spawn arguments, before any child
    exists, so a caller that asked for containment it cannot have learns that
    from the exception instead of from a descendant that outlived its run.

    ``os.name`` is patched rather than the test being Windows-only: the guard
    is the thing under test, and it must hold regardless of which platform
    happens to be running the suite. The Windows job exercises the same helper
    through its real ``os.name``; this pins the branch deterministically
    everywhere. ``monkeypatch`` is what does the patching so the restore is
    guaranteed rather than left to a ``finally``.
    """
    monkeypatch.setattr(os, "name", "nt")
    with pytest.raises(ValueError, match="POSIX process groups"):
        _subprocess_context._ownership_spawn_kwargs(
            ProcessGroupPolicy.OWN_GROUP,
        )


def test_inherited_policy_spawns_no_new_session() -> None:
    """``INHERIT`` implies no spawn keyword, whatever the platform.

    The default has to stay inert: the policy is opt-in, and a spawn flag that
    slipped into the default path would change process topology for every
    existing caller.
    """
    assert not _subprocess_context._ownership_spawn_kwargs(
        ProcessGroupPolicy.INHERIT
    ), "INHERIT must not alter how a child is spawned"


def test_context_defaults_to_the_inherited_policy() -> None:
    """A context states ``INHERIT`` unless the caller says otherwise."""
    assert ExecutionContext().process_group is ProcessGroupPolicy.INHERIT


@dc.dataclass(frozen=True, slots=True)
class _SpawnedArgv:
    """The argv the run handed to the spawn call."""

    argv: tuple[str, ...]
    start_new_session: bool


def test_owned_policy_spawns_the_child_as_a_session_leader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The policy reaches the spawn call, rather than only the teardown.

    Signalling a group the run does not lead would be a containment claim with
    nothing behind it, so the test asserts on the spawn flag itself instead of
    inferring it from teardown behaviour. The child is a real one so the
    recorded call is the production call, not a stub's.
    """
    recorded: list[_SpawnedArgv] = []
    real_spawn = _wait4_process.spawn_direct_process

    async def recording_spawn(
        config: _wait4_process.DirectProcessConfig,
    ) -> asyncio.subprocess.Process:
        """Record the spawn inputs, then spawn for real."""
        recorded.append(_SpawnedArgv(config.argv, config.start_new_session))
        return await real_spawn(config)

    monkeypatch.setattr(_wait4_process, "spawn_direct_process", recording_spawn)

    async def run_case() -> None:
        """Run one command under the owning policy."""
        cmd = sh.make(ECHO)("-n", "owned")
        await cmd.run(
            output=RunOutputOptions(capture=True, echo=False),
            context=ExecutionContext(process_group=ProcessGroupPolicy.OWN_GROUP),
        )

    asyncio.run(run_case())

    assert recorded, "the run must have spawned through the recorded call"
    assert recorded[0].start_new_session, (
        "OWN_GROUP must spawn the child as its own session leader"
    )


def test_the_lines_path_spawns_its_child_as_a_session_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SafeCmd.lines()`` carries the policy too, not only ``run()``.

    The two paths reach the spawn through different code — one builds a
    subprocess execution, the other builds a line-stream run — so a policy
    wired into only one of them would leave the other inheriting silently.
    Iterating a line stream is exactly the case ownership matters for, because
    a line consumer is what outlives a descendant that holds the pipe.

    The child is one that ends immediately: the assertion is about the spawn
    arguments, so the run only has to reach the spawn and finish.
    """
    recorded: list[_SpawnedArgv] = []
    real_spawn = _wait4_process.spawn_direct_process

    async def recording_spawn(
        config: _wait4_process.DirectProcessConfig,
    ) -> asyncio.subprocess.Process:
        """Record the spawn inputs, then spawn for real."""
        recorded.append(_SpawnedArgv(config.argv, config.start_new_session))
        return await real_spawn(config)

    monkeypatch.setattr(_wait4_process, "spawn_direct_process", recording_spawn)

    async def run_case() -> None:
        """Iterate one command's lines under the owning policy."""
        cmd = sh.make(ECHO)("-n", "owned")
        stream = cmd.lines(
            output=RunOutputOptions(capture=True, echo=False),
            context=ExecutionContext(process_group=ProcessGroupPolicy.OWN_GROUP),
        )
        async for _event in stream:
            pass

    asyncio.run(run_case())

    assert recorded, "the run must have spawned through the recorded call"
    assert recorded[0].start_new_session, (
        "OWN_GROUP must spawn a line stream's child as its own session leader"
    )
