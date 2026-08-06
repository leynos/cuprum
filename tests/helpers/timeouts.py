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
    """
    return ("-c", _CHILD_SOURCE, str(marker))


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
    """
    return ("-c", _STUBBORN_SOURCE, str(marker))


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


def started_pids(events: cabc.Iterable[ExecEvent]) -> list[int]:
    """Return the pid of every subprocess that reached the ``start`` phase."""
    return [ev.pid for ev in events if ev.phase == "start" and ev.pid is not None]


def pending_tasks() -> set[asyncio.Task[object]]:
    """Return unfinished tasks on the running loop, excluding the caller.

    A timeout must not strand stream consumers or stdin writers; anything left
    here after a run has unwound is a leak.

    A run creates tasks with genuinely different result types — stream
    consumers yield ``str | None``, the stdin writer and observe hooks yield
    ``None`` — and ``asyncio.Task`` is invariant in that parameter, so no
    single precise element type exists. Callers only count what is left here
    and never read a result, so ``object`` is the narrowest sound annotation;
    the cast records that deliberately rather than widening to ``Any``.
    """
    current = asyncio.current_task()
    tasks = typ.cast("set[asyncio.Task[object]]", asyncio.all_tasks())
    return {task for task in tasks if task is not current and not task.done()}


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
