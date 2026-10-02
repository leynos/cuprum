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
import os
import signal
import sys
import typing as typ

import pytest

from cuprum import ECHO, _subprocess_context, _wait4_process, sh
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


def test_owned_policy_is_rejected_where_posix_groups_do_not_exist() -> None:
    """``OWN_GROUP`` refuses rather than silently containing nothing.

    The refusal is raised while building the spawn arguments, before any child
    exists, so a caller that asked for containment it cannot have learns that
    from the exception instead of from a descendant that outlived its run.

    ``os.name`` is patched rather than the test being Windows-only: the guard
    is the thing under test, and it must hold regardless of which platform
    happens to be running the suite. The Windows job exercises the same helper
    through its real ``os.name``; this pins the branch deterministically
    everywhere, and the patch is restored before anything could spawn.
    """
    original = os.name
    try:
        os.name = "nt"
        with pytest.raises(ValueError, match="POSIX process groups"):
            _subprocess_context._ownership_spawn_kwargs(
                ProcessGroupPolicy.OWN_GROUP,
            )
    finally:
        os.name = original


def test_inherited_policy_spawns_no_new_session() -> None:
    """``INHERIT`` implies no spawn keyword, whatever the platform.

    The default has to stay inert: the policy is opt-in, and a spawn flag that
    slipped into the default path would change process topology for every
    existing caller.
    """
    assert (
        _subprocess_context._ownership_spawn_kwargs(ProcessGroupPolicy.INHERIT) == {}
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
