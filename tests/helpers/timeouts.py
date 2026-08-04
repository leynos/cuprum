"""Shared scaffolding for public-boundary timeout tests.

Used by the command tests in ``test_safe_cmd_run`` and the pipeline tests in
``test_pipeline``. The child writes identifiable output on both streams and
then blocks indefinitely, so a timeout is the only way the run can end.

Nothing here synchronises on elapsed time. A non-positive timeout denotes an
already-elapsed deadline, so expiry is structural rather than raced: the child
blocks on a long sleep purely so it cannot exit of its own accord, and the
readiness marker it writes lets a caller that needs a *started* child wait for
that fact rather than guess at it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

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
    """
    return ("-c", _CHILD_SOURCE, str(marker))


def python_interpreter() -> str:
    """Return the interpreter path used for child processes."""
    return str(sys.executable)


def process_is_running(pid: int) -> bool:
    """Return whether ``pid`` still exists.

    ``_terminate_process`` awaits ``process.wait()`` before the failure
    propagates, so a reaped child is already gone once the caller regains
    control and this reports ``False`` without any polling.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def started_pids(events: cabc.Iterable[typ.Any]) -> list[int]:
    """Return the pid of every subprocess that reached the ``start`` phase."""
    return [ev.pid for ev in events if ev.phase == "start" and ev.pid is not None]


def pending_tasks() -> set[asyncio.Task[typ.Any]]:
    """Return unfinished tasks on the running loop, excluding the caller.

    A timeout must not strand stream consumers or stdin writers; anything left
    here after a run has unwound is a leak.
    """
    current = asyncio.current_task()
    return {
        task for task in asyncio.all_tasks() if task is not current and not task.done()
    }
