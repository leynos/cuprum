"""Shared scaffolding for public-boundary timeout tests.

Used by the command tests in ``test_safe_cmd_run`` and the pipeline tests in
``test_pipeline``. The child writes identifiable output on both streams and
then blocks indefinitely, so a timeout is the only way the run can end.

Nothing here synchronizes on elapsed time. A non-positive timeout denotes an
already-elapsed deadline, so expiry is structural rather than raced: the child
blocks on a long sleep purely so it cannot exit of its own accord, and the
readiness marker it writes lets a caller that needs a *started* child wait for
that fact rather than guess at it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.events import ExecEvent

CHILD_STDOUT = "child-stdout-marker"
CHILD_STDERR = "child-stderr-marker"

# Long enough that the child cannot plausibly exit on its own, so a run that
# ends can only have ended because the deadline expired.
_BLOCK_SECONDS = 300

_CHILD_SOURCE = "; ".join((
    "import sys, pathlib, time",
    f"sys.stdout.write({CHILD_STDOUT!r} + chr(10))",
    "sys.stdout.flush()",
    f"sys.stderr.write({CHILD_STDERR!r} + chr(10))",
    "sys.stderr.flush()",
    "pathlib.Path(sys.argv[1]).write_text('ready')",
    f"time.sleep({_BLOCK_SECONDS})",
))


def child_argv(marker: Path) -> tuple[str, str, str]:
    """Return ``-c`` argv for a child that emits on both streams then blocks.

    ``marker`` is written once both streams have been flushed, so a caller can
    wait on it to know the child is running rather than sleeping arbitrarily.

    Returns
    -------
    tuple[str, str, str]
        ``-c``, the child's source, and the marker path as a string.
    """
    return ("-c", _CHILD_SOURCE, str(marker))


# A bare interpreter importing only stdlib modules was measured at 17 ms
# (19 ms worst of five) on this host. Two seconds is over a hundred times that,
# so the cap only bites if start-up has gone pathological, and it is a cap
# rather than a wait: the marker lands in milliseconds and the run deadline the
# caller sets is what the test actually spends its time on.
MARKER_STARTUP_GRACE_S = 2.0


async def wait_for_marker(marker: Path, *, deadline: float) -> None:
    """Fail unless the child writes its readiness marker before ``deadline``.

    ``deadline`` is a loop time rather than a duration, so a caller can derive
    it from the same instant as its run deadline and reason about both at once.

    Parameters
    ----------
    marker : Path
        The file the child writes once its payload has been flushed.
    deadline : float
        Loop time by which the marker must appear.
    """
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout_at(deadline):
            while loop.time() < deadline:
                # ASYNC240: a child process writes this file, so no asyncio
                # primitive can observe it directly.
                if marker.exists():  # ruff: ignore[blocking-path-method-in-async-function]
                    return
                await asyncio.sleep(0.01)
    except TimeoutError:
        pass
    pytest.fail(f"the child did not write {marker} before its deadline")


# Installs a SIGTERM handler that ignores it, so only the SIGKILL escalation
# can end this child. That makes the grace-period window observable: a teardown
# interrupted before escalation leaves the child running.
_STUBBORN_SOURCE = "; ".join((
    "import signal, sys, time",
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
    "import pathlib",
    "pathlib.Path(sys.argv[1]).write_text('ready')",
    f"time.sleep({_BLOCK_SECONDS})",
))


def stubborn_child_argv(marker: Path) -> tuple[str, str, str]:
    """Return ``-c`` argv for a child that ignores ``SIGTERM`` then blocks.

    Writing ``marker`` after the handler is installed lets a caller wait for
    the child to be genuinely immune before triggering teardown, rather than
    racing the interpreter's start-up.

    Returns
    -------
    tuple[str, str, str]
        ``-c``, the child's source, and the marker path as a string.
    """
    return ("-c", _STUBBORN_SOURCE, str(marker))


def python_interpreter() -> str:
    """Return the interpreter path used for child processes."""
    return str(sys.executable)


# The grandchild inherits its parent's stdout and stderr, so it holds the write
# ends of the run's pipes. It ignores SIGTERM, so only a SIGKILL — to it, or to
# a process group containing it — can end it. It writes its pid only after
# installing its handler, so the pid file's arrival is itself the proof that the
# grandchild is genuinely immune rather than mid-start-up. The pid is staged and
# then renamed into place, so a reader polling for that file can never catch it
# half-written: the expected name appears only once the whole pid is on disk.
_GRANDCHILD_SOURCE = "; ".join((
    "import os, pathlib, signal, sys, time",
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
    "target = pathlib.Path(sys.argv[1])",
    "staged = target.parent / (target.name + '.part')",
    "staged.write_text(str(os.getpid()))",
    "os.replace(staged, target)",
    f"time.sleep({_BLOCK_SECONDS})",
))

# The parent spawns the grandchild and then blocks. It passes its own stdout
# and stderr straight through, which is what leaves the grandchild holding the
# pipe after the parent is gone.
_PARENT_SOURCE = "; ".join((
    "import subprocess, sys, time",
    "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])",
    f"time.sleep({_BLOCK_SECONDS})",
))


def pipe_holding_child_argv(
    pid_file: Path,
) -> tuple[str, ...]:
    """Return ``-c`` argv for a child that holds a pipe through a grandchild.

    The child spawns a grandchild that inherits the run's stdout and stderr and
    ignores ``SIGTERM``, so the grandchild owns the write ends of those pipes
    and no signal aimed at the direct child can dislodge it.

    Parameters
    ----------
    pid_file:
        Path the grandchild writes its own pid to, after installing its
        handler. Its arrival therefore doubles as the readiness signal.

    Returns
    -------
    tuple[str, ...]
        ``-c`` argv, ready to spread into a spawn call.
    """
    return (
        "-c",
        _PARENT_SOURCE,
        _GRANDCHILD_SOURCE,
        str(pid_file),
    )


# A child that starts the pipe-holding grandchild above, waits for it to
# announce itself, and then exits with a chosen code. The readiness wait is
# load-bearing: a stage that exited before its descendant existed would give a
# teardown nothing to reach, and the case would pass while proving nothing.
# The child never waits on the grandchild, so its exit says nothing about it —
# which is exactly the situation a group teardown exists for.
#
# Joined with newlines rather than the semicolons the one-statement sources
# above use, because this child needs a loop and the compound-statement form
# cannot be written on one line. The indentation is part of the source.
#
# A caller may have the child announce itself on stdout first. That write is
# placed ahead of the spawn so it is already sitting in the run's capture
# buffer by the time the leader exits: a capturing run that goes on to time out
# over the descendant still holds text to report, which is what distinguishes a
# payload-preserving expiry from one that discards.
_DESCENDANT_THEN_EXIT_LINES = (
    "import pathlib, subprocess, sys, time",
    "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])",
    "target = pathlib.Path(sys.argv[2])",
    "deadline = time.monotonic() + 10.0",
    "while not target.exists() and time.monotonic() < deadline:",
    "    time.sleep(0.01)",
    "sys.exit(int(sys.argv[3]))",
)


def descendant_then_exit_argv(
    pid_file: Path,
    *,
    exit_code: int,
    stdout_marker: str | None = None,
) -> tuple[str, ...]:
    """Return ``-c`` argv for a child that leaves a pipe-holding descendant behind.

    The child spawns the ``SIGTERM``-immune grandchild described by
    :func:`pipe_holding_child_argv`, waits until the grandchild has recorded
    its pid, and then exits with ``exit_code`` without waiting for it. The
    descendant therefore outlives its parent and still holds the run's
    inherited pipes — the state a direct-child-only teardown cannot clean up.

    Parameters
    ----------
    pid_file:
        Path the grandchild writes its pid to. Its arrival is also the
        readiness signal this child waits on before exiting.
    exit_code:
        Status the child exits with, so a caller can choose between a failing
        stage and a successful one that merely left work behind.
    stdout_marker:
        When given, a line the child writes to stdout and flushes before it
        spawns anything. The text is therefore already readable by the run
        well before the deadline it may go on to overrun.

    Returns
    -------
    tuple[str, ...]
        ``-c`` argv, ready to spread into a spawn call.
    """
    announce = (
        ()
        if stdout_marker is None
        else (
            f"sys.stdout.write({stdout_marker!r} + chr(10))",
            "sys.stdout.flush()",
        )
    )
    lines = (
        *_DESCENDANT_THEN_EXIT_LINES[:1],
        *announce,
        *_DESCENDANT_THEN_EXIT_LINES[1:],
    )
    return (
        "-c",
        "\n".join(lines),
        _GRANDCHILD_SOURCE,
        str(pid_file),
        str(exit_code),
    )


def wait_for_pid_file(path: Path, *, seconds: float = 10.0, context: str) -> int:
    """Return the pid recorded in ``path``, waiting until the file appears.

    The timeout is raised rather than reported through ``pytest.fail`` so the
    function returns an expression on every path; the caller, not this helper,
    decides how a missing pid is reported.

    Returns
    -------
    int
        The pid the process recorded.

    Raises
    ------
    AssertionError
        If the file does not appear within ``seconds``.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text().strip()
            if text:
                return int(text)
        time.sleep(0.05)
    msg = f"Process did not record its pid for {context}"
    raise AssertionError(msg)


def process_is_running(pid: int) -> bool:
    """Return whether ``pid`` still exists.

    ``_terminate_process`` awaits ``process.wait()`` before the failure
    propagates, so a reaped child is already gone once the caller regains
    control and this reports ``False`` without any polling.

    Returns
    -------
    bool
        ``True`` if ``pid`` exists (even if not owned by the caller),
        ``False`` if it has already been reaped.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def started_pids(events: cabc.Iterable[ExecEvent]) -> list[int]:
    """Return the pid of every subprocess that reached the ``start`` phase."""
    return [ev.pid for ev in events if ev.phase == "start" and ev.pid is not None]


def pending_tasks() -> set[asyncio.Task[object]]:
    """Return unfinished tasks on the running loop, excluding the caller.

    A timeout must not strand stream consumers or stdin writers; anything left
    here after a run has unwound is a leak.

    Returns
    -------
    set[asyncio.Task[object]]
        Every task on the running loop that is neither the caller nor
        already done.
    """
    current = asyncio.current_task()
    return {
        task for task in asyncio.all_tasks() if task is not current and not task.done()
    }


def wait_for_process_death(
    pid: int,
    *,
    seconds: float = 5.0,
    context: str = "subprocess termination",
) -> None:
    """Fail unless ``pid`` has gone within ``seconds``.

    Shared by the behavioural runtime tests and the public-boundary timeout
    tests, both of which assert that a terminated child is actually reaped.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not process_is_running(pid):
            return
        time.sleep(0.05)
    pytest.fail(  # pragma: no cover - defensive failure
        f"Process {pid} still alive after {context}",
    )
