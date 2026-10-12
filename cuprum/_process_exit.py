"""Observe a subprocess exit without trusting a stranded wait future.

Two readings live here, and they differ in what they cost the caller.
:func:`_await_process_exit` blocks until the child is gone and is what a
teardown waits on. :func:`_observed_exit_code` only *looks*: it reports how a
child ended while leaving its status for the owning reaper, which is what a
caller needs when it must decide whether the child is still there without
consuming the very status a later wait has to record.
"""

from __future__ import annotations

import asyncio
import os
from asyncio import sleep

_PROCESS_EXIT_INITIAL_POLL_INTERVAL = 0.01
_PROCESS_EXIT_MAX_POLL_INTERVAL = 1.0

#: Whether this platform can observe an exit without reaping the child.
#: ``waitid`` with ``WNOWAIT`` is the POSIX way to ask; a platform without it
#: has no probe here and callers fall back to whatever ``returncode`` says.
_NON_REAPING_PROBE_AVAILABLE = (
    os.name == "posix" and hasattr(os, "waitid") and hasattr(os, "CLD_EXITED")
)


async def _await_process_exit(process: asyncio.subprocess.Process) -> int:
    """Return an exit code from either the waiter or its published value."""
    if process.returncode is not None:
        return process.returncode

    async def _published_returncode() -> int:
        """Poll the authoritative return code with capped exponential backoff."""
        interval = _PROCESS_EXIT_INITIAL_POLL_INTERVAL
        # ASYNC110: Process exposes no completion event, so its published
        # return code is the only non-blocking recovery signal for a lost wake-up.
        while process.returncode is None:
            await sleep(interval)
            interval = min(interval * 2, _PROCESS_EXIT_MAX_POLL_INTERVAL)
        return process.returncode

    wait_task = asyncio.create_task(process.wait())
    published_task = asyncio.create_task(_published_returncode())
    try:
        completed, _ = await asyncio.wait(
            (wait_task, published_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        return wait_task.result() if wait_task in completed else published_task.result()
    finally:
        for task in (wait_task, published_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(wait_task, published_task, return_exceptions=True)


def _observed_exit_code(process: asyncio.subprocess.Process) -> int | None:
    """Return how *process* has already ended, or ``None`` while it runs.

    The published ``returncode`` is authoritative once the owner's reaper has
    recorded it, but it is not the only evidence an exit leaves: an owned child
    that has exited and not yet been reaped is a zombie, and a caller that asks
    the operating system about it learns how it ended without disturbing the
    status the reaper is still going to collect.

    That distinction is the whole reason this function exists. ``returncode``
    is published by whoever owns the reap, so between the child's exit and that
    reaper running, the attribute still reads ``None`` for a child that is
    already gone. A caller deciding whether a signal can still reach the child
    has to look further than the attribute, and must not reap the child to find
    out — the owning reaper needs that status, and collecting it here would
    strand the run's resource usage.

    Returns
    -------
    int | None
        The code the child ended with, a signal-ending reported as the negated
        signal number (so ``-15`` for ``SIGTERM``), or ``None`` when the child
        is still running or its status is no longer observable.
    """
    published = process.returncode
    if published is not None:
        return published
    if not _NON_REAPING_PROBE_AVAILABLE:
        return None
    pid = process.pid
    if pid is None:
        return None
    try:
        info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
    except (ChildProcessError, ProcessLookupError):
        # Either the status was already collected or this process is not the
        # child's parent any more. Both mean there is nothing left to observe,
        # which is what the caller reads this as.
        return None
    if info is None:
        # No child of this pid is in a waitable state, so it is still running.
        return None
    return info.si_status if info.si_code == os.CLD_EXITED else -info.si_status
