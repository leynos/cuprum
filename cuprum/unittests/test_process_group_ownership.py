"""Ownership contains a child's descendants on POSIX, and only those.

A run spawned with ``ProcessGroupPolicy.OWN_GROUP`` owns the process group its
child leads, so teardown can signal the whole group rather than the direct
child alone. These tests assert the difference that makes: a grandchild that
inherits the run's pipe and ignores ``SIGTERM`` is gone once the run is torn
down, the pipe is released, and a process outside the group is untouched.
``INHERIT`` is checked here too, because it must keep signalling only the
direct child. The spawn helpers and how a context resolves the policy itself
live in the sibling support and policy modules.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import typing as typ

import pytest

from cuprum import _group_teardown, _process_group
from cuprum._process_group import _POST_KILL_SETTLEMENT_S
from cuprum._process_lifecycle import _terminate_all_shielded
from cuprum._teardown_policy import _TeardownPolicy
from cuprum.unittests._process_group_ownership_support import (
    _kill_ignoring_missing,
    _posix_only,
    _spawn_immune_child,
    _spawn_owned_run,
    _terminate_owned_run,
)
from tests.helpers.timeouts import (
    process_is_running,
    python_interpreter,
    wait_for_pid_file,
    wait_for_process_death,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

pytestmark = _posix_only


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
    """The drain reaches EOF because no live process owns the write end.

    A reader waiting for end-of-file on the run's stdout cannot settle while
    the grandchild holds that descriptor, so releasing the pipe is what the
    bounded capture grace window otherwise papers over.
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
    belongs to a different group: if teardown signalled anything wider, that
    process — and the test runner — would die with it.
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
    ownership, leaves the grandchild alive, so a bug that contained descendants
    unconditionally could not pass every other test here.
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
            # Guarded because the leader is already reaped: if the assertion
            # above failed because the grandchild had gone, the group is empty
            # and an unguarded ``killpg`` would raise ``ProcessLookupError`` and
            # replace the assertion failure with its own report.
            if process_is_running(run.grandchild_pid):
                _kill_ignoring_missing(run.process.pid, group=True)
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
    finds no one to signal. The teardown absorbs that as it absorbs a signal to
    an already-reaped child, rather than failing the completed run's cleanup.
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


def test_owned_teardown_reaches_a_group_its_reaped_leader_left_behind(
    tmp_path: Path,
) -> None:
    """A reaped leader is not settlement while its group holds a member.

    This is the failure the ownership policy exists to prevent, in its most
    easily missed form. The direct child exits straight away, but a descendant
    stays behind in the group the child led — and the child is reaped by the
    time teardown starts, so a check on the direct child alone would call the
    run settled and never signal the group at all. The kernel keeps the group
    addressable while that member lives, so the signal has somewhere to land;
    a live answer cannot prove that group is this run's, but the probe only
    ever vetoes, so an addressable name is all the teardown needs.
    """

    async def run_case() -> None:
        """Spawn a leader that leaves a member behind and exits, then tear down."""
        pid_file = tmp_path / "member.pid"
        leader_script = (
            "import os, time\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            f"    open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "    while True:\n"
            "        time.sleep(60)\n"
            "os._exit(0)\n"
        )
        process = await asyncio.create_subprocess_exec(
            python_interpreter(),
            "-c",
            leader_script,
            start_new_session=True,
        )
        # The leader exits at once; wait for that exit *and* for the member it
        # left behind to be observable, so teardown genuinely starts against a
        # reaped leader rather than racing its spawn.
        await process.wait()
        member_pid = await asyncio.to_thread(
            wait_for_pid_file,
            pid_file,
            context="member left behind by the reaped leader",
        )
        assert process_is_running(member_pid), (
            "the member must outlive the leader this case is built around"
        )
        assert process.returncode is not None, (
            "the leader must be reaped before teardown begins"
        )

        try:
            await _terminate_all_shielded(
                (process,),
                _TeardownPolicy(0.5, owns_group=True),
            )
            assert not process_is_running(member_pid), (
                "a reaped leader must not settle an owned group that still "
                "holds a descendant"
            )
        finally:
            _kill_ignoring_missing(process.pid, group=True)
            _kill_ignoring_missing(member_pid, group=False)

    asyncio.run(run_case())


def test_repeated_cancellation_does_not_abandon_owned_group_cleanup(
    tmp_path: Path,
) -> None:
    """A cancelled teardown still completes before the cancellation propagates.

    Teardown is wrapped in the shielded primitive precisely so a caller's
    cancellation cannot abandon it part-way and leave an owned group running.
    Cancelling twice pins the escalated case: the first lands while the grace
    period is still counting down.
    """

    async def run_case() -> None:
        """Cancel a teardown twice and confirm it still completed."""
        run = await _spawn_owned_run(tmp_path)
        # Bound before the try so the guard below can tell "not started" from
        # "started and still running"; assigning it inside would leave the
        # cleanup reading an unbound name if the task were never created.
        task: asyncio.Task[None] | None = None
        try:
            task = asyncio.create_task(_terminate_owned_run(run))
            # Let the teardown reach its grace-period wait before interrupting
            # it, so the cancellation lands inside the shielded region we mean
            # to exercise rather than before it starts.
            await asyncio.sleep(0)
            task.cancel()
            # Wait for evidence rather than for an interval. The child dies on
            # the SIGTERM teardown sends it, so its reaping is that delivery
            # observed from outside; nothing else can end it, because it sleeps
            # for far longer than this wait.
            await asyncio.to_thread(
                wait_for_process_death,
                run.process.pid,
                context="owned-group SIGTERM delivery",
            )
            # The grandchild ignores SIGTERM, so the group cannot have settled
            # yet: the grace period is still counting down. Asserting that is
            # what makes the second cancellation below land inside the shielded
            # teardown rather than after it — if the escalation had already
            # run, this fails loudly instead of the test passing without ever
            # interrupting cleanup.
            assert process_is_running(run.grandchild_pid), (
                "the grandchild must outlive the grace phase, so the repeated "
                "cancellation interrupts a teardown that is still working"
            )
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not process_is_running(run.grandchild_pid), (
                "cleanup must finish even when the caller cancels twice"
            )
        finally:
            # The assertions above can fail while the teardown is still in
            # flight, so bound it here rather than leak the task and the
            # descendant it was working on into the rest of the session.
            if task is not None and not task.done():
                task.cancel()
                await asyncio.wait({task}, timeout=_POST_KILL_SETTLEMENT_S)
            # A group outlives its leader only while a member remains, and the
            # kernel keeps the pgid reserved until then — so while the
            # grandchild is up, this is still a group the run owns. Once it is
            # gone the group has ended and its number may have been recycled
            # onto a stranger, which is why the probe comes first.
            if process_is_running(run.grandchild_pid):
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


def test_an_unreapable_group_member_does_not_stall_the_escalation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The post-``SIGKILL`` group wait is bounded, and reports what it left.

    A group member whose parent died first can be re-parented outside this run,
    so its exit is recorded only when something else reaps it. Waiting for that
    without a bound would let a teardown outlive its grace period indefinitely,
    so the wait is capped and the outstanding members reported instead. The
    direct child is still awaited without a bound, and that is the important
    half: a run must reap the process it spawned even when the group around it
    cannot settle, or the caller is left with a zombie it owns.
    """

    async def refusing_group_wait(pgid: int) -> None:
        """Stand in for a group that never reports itself empty.

        The single parameter matches the real probe's, and the teardown gathers
        its targets with ``return_exceptions=True``, so a double with a wider
        signature would raise into that gather and the test would pass without
        the bound ever engaging.
        """
        del pgid
        await asyncio.sleep(3600)

    async def escalate_against_a_group_that_never_settles(
        process: asyncio.subprocess.Process,
    ) -> float:
        """Tear the child down and return how long the escalation took."""
        # The grace period has to be short enough to take the escalation
        # promptly but non-zero: a zero-length wait is a corner case that can
        # complete without ever suspending, which would settle the run on the
        # first phase and skip the bounded wait this test exists to pin.
        started = time.monotonic()
        # The reporting module writes this record, not the module that decided
        # to signal: binding here to ``_process_signal`` would capture nothing
        # and the assertion below would fail for the wrong reason.
        with caplog.at_level(logging.WARNING, logger=_group_teardown.__name__):
            await _terminate_all_shielded(
                (process,),
                _TeardownPolicy(0.1, owns_group=True),
            )
        return time.monotonic() - started

    # The cap lives with the poll it bounds, so the double replaces the poll
    # itself: patching a name the capped wrapper does not call would leave the
    # real loop running and this test would pass without the bound engaging.
    monkeypatch.setattr(_process_group, "_await_group_exit", refusing_group_wait)

    async def run_case() -> None:
        """Escalate against a group that will never settle."""
        process = await _spawn_immune_child(tmp_path / "immune.ready")
        elapsed = await escalate_against_a_group_that_never_settles(process)
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
