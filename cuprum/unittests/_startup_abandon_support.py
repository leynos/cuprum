"""The children and probes the startup-abandonment cases need to be real.

A reclaim that must stop, reap, and release a live child cannot be judged
against a stub: its guarantees are about process lifetime and descriptors, and
a fake process holds neither. What the cases share is therefore the machinery
for owning a real child, not a substitute for one.

The scenario here is deliberately adversarial. A child spawns a descendant that
inherits its output pipes and asks for its own session, then waits to be
signalled. Terminating the child leaves both write ends in the descendant's
hands, so no end-of-file ever arrives and the reclaim's bounded discard expires
still holding the parent's read descriptors. That is what separates "the child
was reaped" from "the parent's pipes were released", which the ``wait4`` owner
does as two unrelated acts.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import] - the child is built to inherit the parent's pipes.
import sys
import time
import typing as typ

from cuprum._constants import STDERR_STREAM, STDOUT_STREAM
from cuprum._wait4_spawn import DirectProcessConfig, spawn_wait4_process

if typ.TYPE_CHECKING:
    import pathlib

__all__ = [
    "_is_fd_open",
    "_is_running",
    "_kill_descendant",
    "_recorded_pid",
    "_spawn_output_pipe_holder",
    "_wait_for_pid_file",
]

#: Spawns a descendant that inherits this child's output pipes, records its
#: pid at ``sys.argv[1]``, and then waits to be signalled. The descendant asks
#: for its own session, so it survives the direct child's termination and the
#: reclaim cannot reach it by signalling a group. The lifetime comes from
#: ``sys.argv[2]`` so the case can size it against its own bound, and nothing
#: reads the pid back except the cleanup that kills the descendant and the
#: assertion that it outlived the reclaim.
_DESCENDANT_SCRIPT = """\
import pathlib
import subprocess
import sys
import time

lifetime = sys.argv[2]
descendant = subprocess.Popen(
    [sys.executable, "-c", f"import time; time.sleep({lifetime})"],
    stdin=subprocess.DEVNULL,
    stdout=sys.stdout,
    stderr=sys.stderr,
    start_new_session=True,
)
pathlib.Path(sys.argv[1]).write_text(str(descendant.pid), encoding="ascii")
time.sleep(float(lifetime))
"""


async def _spawn_output_pipe_holder(
    pid_file: pathlib.Path,
    lifetime_s: float,
) -> asyncio.subprocess.Process:
    """Spawn a child whose descendant inherits its output pipes.

    Parameters
    ----------
    pid_file : pathlib.Path
        Where the child records the descendant's pid, so the case can ask
        whether the descendant is still alive and can kill it afterwards.
    lifetime_s : float
        How long both the child and its descendant wait before exiting. The
        case sizes it so that a reclaim which waited for either to exit would
        miss its own bound.

    Returns
    -------
    asyncio.subprocess.Process
        The child, spawned through the ``wait4`` route with stdout and stderr
        piped to the parent.
    """
    return await spawn_wait4_process(
        DirectProcessConfig(
            argv=(
                sys.executable,
                "-c",
                _DESCENDANT_SCRIPT,
                str(pid_file),
                str(lifetime_s),
            ),
            stdin=None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=None,
            cwd=None,
            pipes=frozenset({STDOUT_STREAM, STDERR_STREAM}),
        ),
    )


def _recorded_pid(path: pathlib.Path) -> int | None:
    """Return the recorded descendant pid, or ``None`` while it is unwritten."""
    # A blocking read, kept out of a case's coroutine for the same reason
    # ``_spill_files`` is: it would stall the loop the case is watching.
    try:
        return int(path.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None


async def _wait_for_pid_file(path: pathlib.Path, timeout_s: float) -> int:
    """Wait until the child has recorded its descendant's pid, and return it."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pid = _recorded_pid(path)
        if pid is not None:
            return pid
        await asyncio.sleep(0.01)
    msg = f"{path} was not written within {timeout_s}s"
    raise AssertionError(msg)


def _is_fd_open(fd: int) -> bool:
    """Report whether *fd* still names an open descriptor in this process."""
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def _is_running(pid: int) -> bool:
    """Report whether *pid* still names a live process."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _kill_descendant(pid: int | None) -> None:
    """Kill the descendant a case spawned, so a failed case leaks nothing.

    The descendant is not this process's child — it was reparented when the
    direct child died — so there is nothing here to reap, and signalling it by
    the pid the child recorded is the only handle the case has.
    """
    if pid is None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
