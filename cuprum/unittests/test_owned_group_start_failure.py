"""An owning run's failed start must stop the descendant it left in its group.

``OWN_GROUP`` makes the teardown signal the child's process group rather than
the single pid. The existing owned-group case proves the child itself is
reaped; it cannot prove the group was signalled, because a single-pid teardown
would reap that child just as well. This case puts a descendant in the same
group and leaves it running when the ``start`` hook raises, so only a teardown
that carries the run's ownership reaches it.

The descendant deliberately does not start a new session, unlike the
inherited-pipe case's descendant: staying in the group is the whole point. The
hook waits for the descendant's pid to be recorded before it raises, so the
descendant is provably alive when the reclaim begins rather than racing it.
"""

from __future__ import annotations

import asyncio
import time
import typing as typ

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum.sh import ExecutionContext, ProcessGroupPolicy, RunOutputOptions
from cuprum.unittests._start_ownership_support import (
    _assert_strictly_reaped,
    _python_catalogue,
    _StartHookError,
)
from cuprum.unittests._startup_abandon_support import (
    _kill_descendant,
    _posix_only,
    _recorded_pid,
)
from cuprum.unittests._wait4_process_support import _abandon, _wait4_only
from tests.helpers.timeouts import pending_tasks, started_pids, wait_for_process_death

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    from cuprum.catalogue import ProgramCatalogue
    from cuprum.events import ExecEvent

#: Spawns a descendant that stays in this child's process group, records its
#: pid at ``sys.argv[1]``, then blocks. The descendant is what a single-pid
#: teardown would miss, so its death is the evidence that the group was
#: signalled.
_GROUP_DESCENDANT_CHILD = """\
import pathlib
import subprocess
import sys
import time

descendant = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(300)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
pathlib.Path(sys.argv[1]).write_text(str(descendant.pid), encoding="ascii")
time.sleep(300)
"""

#: How long the hook waits for the child to record its descendant's pid. The
#: child only starts an interpreter and forks, but this host is shared.
_PID_FILE_WAIT_S = 10.0


def _command(*argv: str) -> tuple[sh.SafeCmd, ProgramCatalogue]:
    """Build a Python command and the allowlist catalogue that admits it."""
    program, catalogue = _python_catalogue("owned-group-start-failure")
    return sh.make(program, catalogue=catalogue)(*argv), catalogue


def _wait_for_file(path: pathlib.Path, timeout_s: float) -> None:
    """Block until *path* exists, so the descendant is known to be running.

    A synchronous poll is deliberate: this runs inside an observe hook, which
    must not await. The child is a separate process, so its progress does not
    depend on this thread yielding.

    Raises
    ------
    AssertionError
        If *path* does not appear within *timeout_s*.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    msg = f"{path} was not written within {timeout_s}s"
    raise AssertionError(msg)


def _fail_start_once_the_descendant_is_recorded(
    events: list[ExecEvent],
    pid_file: pathlib.Path,
) -> cabc.Callable[[ExecEvent], None]:
    """Return a hook that records events and fails after the descendant exists."""

    def hook(event: ExecEvent) -> None:
        """Wait for the recorded pid on ``start``, then fail."""
        events.append(event)
        if event.phase == "start":
            _wait_for_file(pid_file, _PID_FILE_WAIT_S)
            raise _StartHookError

    return hook


@_posix_only
@_wait4_only
def test_owned_group_start_failure_stops_the_descendant(tmp_path: pathlib.Path) -> None:
    """A failed owned start must signal the group, not only the child.

    The descendant is in the child's group and outlives the child's own
    termination, so a teardown that signalled only the announced pid would
    leave it running. Its death is therefore the difference between carrying
    the run's ownership and falling back to the default.
    """
    pid_file = tmp_path / "descendant.pid"
    command, catalogue = _command("-c", _GROUP_DESCENDANT_CHILD, str(pid_file))
    events: list[ExecEvent] = []

    async def run_case() -> set[asyncio.Task[object]]:
        """Run the owned command and report the loop's leftovers."""
        with pytest.raises(_StartHookError):
            await command.run(
                output=RunOutputOptions(capture=False, echo=False),
                context=ExecutionContext(
                    process_group=ProcessGroupPolicy.OWN_GROUP,
                ),
            )
        return pending_tasks()

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(_fail_start_once_the_descendant_is_recorded(events, pid_file)),
    ):
        still_pending = asyncio.run(run_case())

    pids = started_pids(events)
    descendant_pid = _recorded_pid(pid_file)
    try:
        assert len(pids) == 1, (
            f"exactly one child must have been announced, got {pids!r}"
        )
        assert not still_pending, (
            f"a failed owned start must leave no task pending, got {still_pending!r}"
        )
        assert descendant_pid is not None, "the hook must have seen the pid file"
        _assert_strictly_reaped(pids[0])
        wait_for_process_death(
            descendant_pid,
            context="a failed owned start hook",
        )
    finally:
        for pid in pids:
            _abandon(pid)
        _kill_descendant(descendant_pid)
