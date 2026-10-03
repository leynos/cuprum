"""The process-group liveness probe that settles an owned teardown.

Teardown of an owned group has to know when that group has emptied, and it
answers that by asking the kernel rather than by tracking members. These tests
pin the three answers the probe can give — members remain, the group is gone,
and the group holds members this process may not signal — and the wait built on
top of them.
"""

from __future__ import annotations

import asyncio
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, no shell
import sys

import pytest

from cuprum._process_group import (
    _await_group_exit,
    _await_group_teardown,
    _group_has_members,
)

# The probe is a POSIX process-group probe; there is no group to ask about
# anywhere else.
_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups are unavailable on Windows",
)

pytestmark = _posix_only


def _spawn_blocking_session_leader() -> subprocess.Popen[bytes]:
    """Start a process that leads its own group and blocks until killed."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        start_new_session=True,
    )


def test_a_live_leaders_group_still_has_members() -> None:
    """The probe reports a group whose leader is running."""
    leader = _spawn_blocking_session_leader()
    try:
        assert _group_has_members(leader.pid), (
            "a running session leader's group must report members"
        )
    finally:
        leader.kill()
        leader.wait()


def test_a_group_whose_members_have_gone_has_none() -> None:
    """Once the last member is reaped, the group ceases to exist.

    This is what makes the probe definitive: there is no stale group record to
    grow back into a false positive, so an owned teardown that observes this
    cannot be waiting on a group that has actually emptied.
    """
    leader = _spawn_blocking_session_leader()
    leader.kill()
    leader.wait()
    assert not _group_has_members(leader.pid), "a reaped group must report no members"


def test_members_this_process_may_not_signal_are_not_awaited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable group reports settled, because waiting cannot reach it.

    ``killpg`` raising ``PermissionError`` means members exist that neither the
    grace period nor the ``SIGKILL`` escalation could signal. Treating that as
    "still running" would make teardown wait out a process it can never end, so
    the probe reports the group as having nothing left to wait for.
    """

    def unreachable(pgid: int, sig: int) -> None:
        """Stand in for a group owned by another user."""
        del pgid, sig
        raise PermissionError

    monkeypatch.setattr(os, "killpg", unreachable)
    assert not _group_has_members(1234), (
        "a group this process cannot signal must not be awaited"
    )


def test_the_wait_returns_once_the_group_empties() -> None:
    """The wait polls until the last member is gone, then returns.

    The leader holds the group open for a moment and is then killed from
    another thread, so the wait has to observe the group emptying rather than
    return on its first probe.

    The killer reaps as well as signals, and that is not incidental: an
    un-reaped zombie still counts as a member of its group, so a probe alone
    would never see the group empty. Reaping the direct child is what closes
    the group, which is why the production teardown awaits the child's exit in
    the same breath as the group's.
    """

    async def run_case() -> None:
        """Wait on a group that empties only after the wait has begun."""
        leader = _spawn_blocking_session_leader()

        def kill_and_reap() -> None:
            """End the leader and reap it, so the group can cease to exist."""
            leader.kill()
            leader.wait()

        try:
            assert _group_has_members(leader.pid), (
                "the group must start with a member, or this test proves nothing"
            )
            killer = asyncio.get_running_loop().call_later(
                0.2,
                kill_and_reap,
            )
            try:
                await asyncio.wait_for(_await_group_exit(leader.pid), timeout=10.0)
            finally:
                killer.cancel()
            assert not _group_has_members(leader.pid), (
                "the wait must not return until the group has emptied"
            )
        finally:
            leader.kill()
            leader.wait()

    asyncio.run(run_case())


def test_a_failing_child_wait_leaves_no_group_poller_behind() -> None:
    """A failed child waiter cancels the group poller instead of stranding it.

    ``asyncio.gather`` propagates the first exception without cancelling its
    siblings. The group poller here is still waiting on a live group when the
    child's waiter fails, so without an explicit cancel it would poll for the
    lifetime of the process — a leak that only shows up when the pooled exit
    path is the one that raises.
    """

    async def run_case() -> None:
        """Fail one waiter while the group poller is still mid-wait."""
        leader = _spawn_blocking_session_leader()
        try:

            async def failing_exit() -> int:
                """Fail while the group is still open."""
                # Yield first so the failure arrives the way a real waiter's
                # would: after the group poller is already running, not before
                # ``gather`` has scheduled it.
                await asyncio.sleep(0)
                msg = "pooled exit failed"
                raise RuntimeError(msg)

            with pytest.raises(RuntimeError, match="pooled exit failed"):
                await asyncio.wait_for(
                    _await_group_teardown(failing_exit(), leader.pid),
                    timeout=10.0,
                )
            assert _group_has_members(leader.pid), (
                "the group must still be open, or this test cannot observe a "
                "stranded poller"
            )
        finally:
            leader.kill()
            leader.wait()

        # Anything the teardown left polling would still be running here.
        leftovers = [
            task
            for task in asyncio.all_tasks()
            if not task.done() and task is not asyncio.current_task()
        ]
        assert not leftovers, (
            f"a failed exit waiter must not strand a group poller; found {leftovers!r}"
        )

    asyncio.run(run_case())
