"""Shared helpers for the scripted live-byte scenarios.

The scenarios are split across modules to stay inside the 400-line ceiling of
the strictly linted helper tree: the portable exchange stories live in
:mod:`tests.helpers.live_byte_scenarios`, the cancellation and stall stories in
:mod:`tests.helpers.live_byte_blocking_scenarios`, the POSIX descendant stories
in :mod:`tests.helpers.live_byte_descendant_scenarios`, and the held-handle
failure stories in :mod:`tests.helpers.live_byte_failure_scenarios`. All of
them need the same child builder, the same reader helpers, the same
bounded-poll helper for a PID that asyncio cannot observe, and the same
predicate that turns an observation into a failing scenario.

Nothing here starts a scenario; the supervisor invokes the scenario module.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

from cuprum import ByteStreamReader, Program, ProgramCatalogue, sh

__all__ = [
    "DESCENDANT_WAIT_S",
    "HANDLE_DEADLINE_S",
    "PAYLOAD_SIZE",
    "PYTHON",
    "REAP_TIMEOUT_S",
    "SETTLEMENT_DEADLINE_S",
    "is_alive",
    "kill_and_reap",
    "open_fd_count",
    "read_all",
    "read_until",
    "require",
    "require_clean_teardown",
    "require_posix_probe",
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


def require_posix_probe() -> None:
    """Refuse a PID-based liveness check on a platform without one.

    The signal-zero probe is a POSIX operation. On Windows the standard library
    routes ``os.kill(pid, 0)`` to ``GenerateConsoleCtrlEvent``, which sends
    Ctrl+C to the child's console group instead of asking whether it exists,
    and it provides no non-signalling replacement. Scenarios that must observe
    a raw PID are therefore POSIX-only, and this refuses rather than silently
    signalling an unrelated process.

    Raises
    ------
    RuntimeError
        If the platform is not POSIX.
    """
    if os.name != "posix":
        message = "a raw-PID liveness probe is only available on POSIX"
        raise RuntimeError(message)


def is_alive(pid: int) -> bool:
    """Return whether *pid* still exists, without signalling it."""
    require_posix_probe()
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
    require_posix_probe()
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    wait_dead(pid, REAP_TIMEOUT_S)


async def read_all(reader: ByteStreamReader) -> bytes:
    """Consume one reader to its end and return the reconstructed bytes."""
    received = bytearray()
    async for chunk in reader:
        received.extend(chunk.data)
    return bytes(received)


async def read_until(reader: ByteStreamReader, marker: bytes) -> tuple[bytes, bytes]:
    """Consume until *marker* appears, returning head and leftover bytes."""
    collected = bytearray()
    async for chunk in reader:
        collected.extend(chunk.data)
        if marker in collected:
            end = collected.index(marker) + len(marker)
            return bytes(collected[:end]), bytes(collected[end:])
    message = f"stream ended before {marker!r}: {bytes(collected)!r}"
    raise RuntimeError(message)


def open_fd_count() -> int | None:
    """Count this process's descriptors, or return ``None`` without procfs."""
    try:
        return sum(1 for _ in Path("/proc/self/fd").iterdir())
    except OSError:
        return None


def require_clean_teardown(before: int | None) -> None:
    """Fail if teardown left an owned task or descriptor behind."""
    pending = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    ]
    require(not pending, f"owned tasks were left pending: {pending!r}")
    if before is not None:
        after = open_fd_count()
        require(after == before, f"descriptors changed from {before} to {after}")
