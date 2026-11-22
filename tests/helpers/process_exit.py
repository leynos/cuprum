"""Decide whether a process the caller does not own has exited.

A test that kills a process tree has to ask afterwards whether a descendant is
gone, and the obvious probe gives the wrong answer. ``kill(pid, 0)`` succeeds
for a zombie exactly as it does for a running process, and a descendant whose
parent died is reaped by whichever process adopts it, on that process's
schedule. A test that waits for the signal probe to fail is therefore timing
its host's reaper rather than the teardown it meant to check.

:func:`observe_exit` asks the scheduler state instead, by one of two routes.
Linux procfs is preferred because it needs no child process. Where procfs
cannot be read — macOS and the BSDs have none — it asks ``ps`` for the same
state letter. A zombie counts as exited on both routes.

The answer has three values, not two, because a host that can answer neither
way has said nothing about the process. Folding that into ``RUNNING`` would
fail a teardown that worked, and folding it into ``EXITED`` would pass one that
did not, so it is reported as :attr:`ExitObservation.UNOBSERVABLE` for the
caller to act on.
"""

from __future__ import annotations

import enum
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed ps argv.

from tests.helpers.process_state import process_state

__all__ = ["ExitObservation", "observe_exit"]

# ``ps`` answers from the process table at once; the bound only keeps a wedged
# host from hanging the test that asked.
_PS_TIMEOUT_S = 5.0
# What ``ps`` exits with when no process matched the selection, on both procps
# and the BSD implementation macOS ships.
_PS_NO_MATCH = 1


class ExitObservation(enum.Enum):
    """What the host reported about whether a process has exited."""

    RUNNING = enum.auto()
    EXITED = enum.auto()
    UNOBSERVABLE = enum.auto()


def _observe_exit_through_ps(pid: int) -> ExitObservation:
    """Classify *pid* from the state letter ``ps`` prints for it."""
    try:
        completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed ps argv.
            ["ps", "-o", "stat=", "-p", str(pid)],  # ruff: ignore[start-process-with-partial-path] - ps is resolved from PATH on every supported host.
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ExitObservation.UNOBSERVABLE
    state = completed.stdout.strip()
    if completed.returncode == 0 and state:
        is_zombie = state.startswith("Z")
        return ExitObservation.EXITED if is_zombie else ExitObservation.RUNNING
    if completed.returncode == _PS_NO_MATCH and not state:
        return ExitObservation.EXITED
    return ExitObservation.UNOBSERVABLE


def observe_exit(pid: int) -> ExitObservation:
    """Report whether *pid* has exited, counting an unreaped zombie as exited.

    Parameters
    ----------
    pid:
        The process to ask about. It need not be a child of the caller.

    Returns
    -------
    ExitObservation
        ``EXITED`` for a zombie or a pid with no process, ``RUNNING`` for a
        process in any other state, and ``UNOBSERVABLE`` when neither procfs
        nor ``ps`` could answer.

    Examples
    --------
    >>> import os
    >>> observe_exit(os.getpid()) is ExitObservation.RUNNING
    True
    """
    state = process_state(pid)
    if state.available:
        return ExitObservation.EXITED if state.exited else ExitObservation.RUNNING
    return _observe_exit_through_ps(pid)
