"""Observe a subprocess exit without trusting a stranded wait future.

Four readings live here, and they differ in how much they disturb what they
read. :func:`_await_process_exit` blocks until the child is gone and is what a
teardown waits on. :func:`_observed_exit_code` only *looks*: it reports how a
child ended while leaving its status for the owning reaper, which is what a
caller needs when it must decide whether the child is still there without
consuming the very status a later wait has to record.
:func:`_child_was_collected` asks a narrower question of the same kernel
interface — not how the child ended, but whether this process can still wait
for it at all — which is what a delivery decision needs before it signals a
PID by number. :func:`_await_child_waitable` is the blocking counterpart of
that last question: it waits for the child to *become* waitable without
collecting anything, which is how a reaper gets the child to the point where
its collection takes no time at all.

The four share one rule, and it is the reason they are kernel probes rather
than ``returncode`` checks. ``returncode`` is published by whoever owns the
reap, so it lags the kernel by however long that owner takes to record what it
collected. Each reading here asks the kernel directly, so none of them can be
fooled by that lag — and the three that ask about state rather than outcome use
``WNOWAIT`` so they never collect what they asked about.
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


def _child_was_collected(pid: int) -> bool:
    """Report whether this process has stopped being able to wait for *pid*.

    This is :func:`_observed_exit_code`'s kernel question asked for a
    different purpose. That probe reports *how* a child ended and folds two
    very different answers into ``None``: "still running" and "the status is
    already gone". A caller deciding whether a signal can still be delivered
    must tell those apart, because they demand opposite responses. A running
    child is reachable, and a zombie whose status is uncollected still holds
    its PID against reuse, so a signal to either is meaningful or harmlessly
    discarded. A child whose status has already been collected is neither: the
    kernel is free to have handed its PID to an unrelated process, and a
    signal sent by number could arrive somewhere else entirely.

    Only the ``ChildProcessError`` distinguishes them. ``waitid`` raises it
    when no waitable child of this process exists at that PID, which is
    exactly the state a completed collection leaves behind; a running child
    answers ``None`` under ``WNOHANG`` and a zombie answers with its record.

    That error does not distinguish a collected child from a PID that was
    never this process's child at all, and this function does not pretend
    otherwise. The looseness is safe because of where the question is asked:
    callers pass a PID this owner spawned, so the second case does not arise,
    and were it to, the answer errs towards refusing a delivery rather than
    towards signalling a PID that may now name a stranger.

    Asking changes nothing and waits for nothing. ``WNOWAIT`` leaves the
    status for the reaper that owns it, and ``WNOHANG`` means this returns at
    once rather than blocking on a child that has not exited — which is what
    lets a signalling guard consult the kernel without either stealing the
    reap or stalling.

    Parameters
    ----------
    pid : int
        The process to ask about. The caller owns it, which is what makes the
        question answerable in the first place.

    Returns
    -------
    bool
        ``True`` when no waitable child remains at *pid*, so a delivery can no
        longer be established. ``False`` when one does, or when this platform
        cannot ask — an unanswerable question must not be reported as proof
        that the child is gone.
    """
    if not _NON_REAPING_PROBE_AVAILABLE:
        return False
    try:
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
    except (ChildProcessError, ProcessLookupError):
        return True
    return False


def _await_child_waitable(pid: int) -> bool:
    """Block until *pid* has exited, without collecting its status.

    This exists so a reaper can be made uncontended for the instant that
    matters. :func:`_child_was_collected` answers the same question without
    waiting, which is what a delivery decision needs — it must refuse *now*
    rather than block — but a reaper deciding whether to take an exclusive lock
    wants the opposite trade. When this reports ``True`` the child is a zombie
    that is still waitable, so the ``wait4`` that follows does not block: it
    runs to completion inside a critical section that no delivery can lengthen.
    When it reports ``False``, nothing was established and the caller must not
    take the collection to be uncontended.

    Waiting *here* rather than inside the critical section is the whole point.
    A lock held across a live child's lifetime would stall every delivery for
    as long as the child happened to run, turning a fast refusal into a slow
    one and — worse — making a delivery to a perfectly healthy child wait on a
    reap it has no relationship to. Holding it only from a confirmed zombie to
    the collected status is a window measured in instructions.

    Dropping ``WNOHANG`` is what makes this block, and ``WNOWAIT`` is what
    keeps it from collecting. Both matter: without ``WNOWAIT`` this would
    consume the very status the reaper is about to collect, and the run's exit
    code and resource usage would be lost; without the blocking form there
    would be nothing here that a non-reaping probe does not already do.

    The wait is interruptible by a signal, and Python retries it where the
    caller has no handler — ``waitid`` is one of the calls ``PEP 475`` gives
    automatic ``EINTR`` retry, so a signal delivered to this process does not
    surface here as a failure.

    Parameters
    ----------
    pid : int
        The child to wait for. The caller owns it, so the kernel has a
        waitable child at this PID to wait on.

    Returns
    -------
    bool
        ``True`` once the child is waitable, and ``False`` in the two cases
        where readiness was *not* established: this platform cannot ask, or
        nothing waitable remains because the status was collected by someone
        else while this owner waited. Both mean the caller must not treat the
        child as ready, and a caller in that position falls back to whatever
        its own reap can establish.
    """
    if not _NON_REAPING_PROBE_AVAILABLE:
        return False
    try:
        os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
    except (ChildProcessError, ProcessLookupError):
        # Nothing waitable remains, which means someone collected it while
        # this owner was waiting for it. Reporting ``False`` rather than
        # raising keeps that outcome with the caller that owns the reap: it
        # is the caller's invariant news, not this probe's error.
        return False
    return True


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
