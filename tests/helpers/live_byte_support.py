"""Shared helpers for the scripted live-byte scenarios.

The scenarios are split across two modules to stay inside the 400-line ceiling
of the strictly linted helper tree: the portable exchange and lifecycle stories
live in :mod:`tests.helpers.live_byte_scenarios`, and the POSIX descendant
stories live in :mod:`tests.helpers.live_byte_descendant_scenarios`. Both need
the same child builder, the same bounded-poll helper for a PID that asyncio
cannot observe, and the same predicate that turns an observation into a
failing scenario.

Nothing here starts a scenario; the supervisor invokes the scenario module.
"""

from __future__ import annotations

import os
import signal
import sys
import time

from cuprum import Program, ProgramCatalogue, sh

__all__ = [
    "DESCENDANT_WAIT_S",
    "HANDLE_DEADLINE_S",
    "PAYLOAD_SIZE",
    "PYTHON",
    "REAP_TIMEOUT_S",
    "SETTLEMENT_DEADLINE_S",
    "is_alive",
    "kill_and_reap",
    "require",
    "wait_dead",
]

#: The payload used by the large-output and stalled-consumer stories.
PAYLOAD_SIZE = 128 * 1024
#: A settlement that takes longer than this has failed its own bound.
SETTLEMENT_DEADLINE_S = 8.0
#: How long a POSIX descendant may take to disappear after its group stop.
DESCENDANT_WAIT_S = 3.0
#: How long a failsafe kill waits for a recorded descendant to disappear.
REAP_TIMEOUT_S = 2.0
#: The child-side deadline used by the stories that need one.
HANDLE_DEADLINE_S = 10.0

_CATALOGUE = ProgramCatalogue.from_programs(sys.executable, name="live-scenarios")
PYTHON = sh.make(Program(sys.executable), catalogue=_CATALOGUE)


def require(
    condition: bool,  # ruff: ignore[boolean-type-hint-positional-argument] - a predicate and its explanation are both required; keyword-only would only lengthen every call.
    message: str,
) -> None:
    """Raise when a scenario observation does not hold."""
    if not condition:
        raise RuntimeError(message)


def is_alive(pid: int) -> bool:
    """Return whether *pid* still exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_dead(pid: int, timeout: float) -> bool:
    """Wait up to *timeout* seconds for *pid* to disappear."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.05)
    return not is_alive(pid)


def kill_and_reap(pid: int) -> None:
    """Kill a surviving descendant and wait for it to disappear."""
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    wait_dead(pid, REAP_TIMEOUT_S)
