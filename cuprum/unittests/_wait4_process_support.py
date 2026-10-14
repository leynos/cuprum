"""Children, spawn inputs, and process probes for the ``wait4`` path's tests.

Every case that drives ``cuprum._wait4_process`` directly spawns a real child,
because the guarantees under test are about process lifetime: a cleanup must
leave no zombie, a mismatched reap must not adopt another child's status, and a
refused delivery must agree with what the kernel actually did. A fake process
can stand for none of those, so what the cases share is not a stub but the
machinery for owning a real child and asking the kernel about it afterwards.

The probes here are deliberately *not* the ones the code under test uses. A
case that judged the owner's probe with the owner's own probe would report
whatever the code believes about itself; these read the process table instead,
so a claim about a child having been reaped is answered by the kernel rather
than by the component being judged.
"""

from __future__ import annotations

import contextlib
import enum
import os
import signal
import sys
import time
import types

import pytest

from cuprum import _process_exit, _wait4_process, _wait4_spawn
from tests.helpers.process_state import process_state

__all__ = [
    "_EXITED",
    "_SLEEP_FOREVER",
    "_ChildOwnership",
    "_FakeUsage",
    "_abandon",
    "_exiting_config",
    "_probe_child",
    "_sleep_config",
    "_wait4_only",
    "_wait_until_exited_unreaped",
    "_without_the_probe",
]

_wait4_only = pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="asserts direct-child wait4 process ownership",
)

# A child that outlives the test until something reaps or signals it, so a
# missing cleanup shows up as a live process rather than a fast exit. The odd
# duration is deliberate: it is long enough to outlive any run here and
# distinctive enough that a process a failed run did strand can be told apart
# from every other sleeper on a shared machine.
_SLEEP_FOREVER = ("sleep", "997")

#: Status the short-lived children below choose for themselves. Non-zero and
#: not a signal's negation, so a reading that guessed or defaulted could not
#: match it.
_EXITED = 21

#: How long a case waits for a child to reach the window it is observing before
#: reporting that it never arrived. Generous, because the child only has to
#: start up and exit for the window to open; what the wait removes is the race
#: against that start-up, not a slow operation.
_EXIT_WINDOW_TIMEOUT = 5.0


class _FakeUsage(types.SimpleNamespace):
    """The resource record shape ``wait4`` returns, with placeholder figures.

    It subclasses ``SimpleNamespace`` rather than merely resembling it so the
    test's stub matches the type its call site declares, and the mismatch this
    test provokes is the pid — not the usage shape.
    """

    def __init__(self) -> None:
        """Populate the three fields ``resource_usage_from_wait4`` reads."""
        super().__init__(ru_maxrss=4, ru_utime=0.5, ru_stime=0.25)


def _sleep_config() -> _wait4_spawn.DirectProcessConfig:
    """Build spawn inputs for a child that stays alive until signalled.

    Returns
    -------
    DirectProcessConfig
        Inputs that request no pipes, keeping the spawn free of stream setup.
    """
    return _wait4_spawn.DirectProcessConfig(
        argv=_SLEEP_FOREVER,
        stdin=None,
        stdout=None,
        stderr=None,
        env=None,
        cwd=None,
    )


def _exiting_config() -> _wait4_spawn.DirectProcessConfig:
    """Build spawn inputs for a child that exits at once with ``_EXITED``."""
    return _wait4_spawn.DirectProcessConfig(
        argv=(sys.executable, "-c", f"import sys; sys.exit({_EXITED})"),
        stdin=None,
        stdout=None,
        stderr=None,
        env=None,
        cwd=None,
    )


class _ChildOwnership(enum.Enum):
    """What a ``WNOHANG`` probe found at a pid this test spawned."""

    #: Still running: nothing has collected it and it has not exited.
    LIVE = enum.auto()
    #: Exited but unreaped. The probe itself reaped it, so the pid is now a
    #: free slot the kernel can hand to an unrelated process.
    REAPED = enum.auto()
    #: Not this process's child at all; someone else collected it first.
    COLLECTED = enum.auto()


def _probe_child(pid: int) -> _ChildOwnership:
    """Report what this process still owns at ``pid``.

    A successful ``waitpid`` under ``WNOHANG`` returns the pid once the child
    has exited, and reaps it as a side effect; it returns ``(0, 0)`` while the
    child is still running, having reaped nothing. Only the exception proves
    the child was collected by someone else. The two non-exceptional outcomes
    are therefore not the same answer, and the caller has to act on them
    differently: a live child is ours to kill, whereas a pid the probe reaped
    is already collected and — because that slot is now available — must not
    be signalled again.

    Returns
    -------
    _ChildOwnership
        ``COLLECTED``, ``REAPED``, or ``LIVE``, in that order of precedence.
    """
    try:
        waited_pid, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return _ChildOwnership.COLLECTED
    if waited_pid == pid:
        return _ChildOwnership.REAPED
    return _ChildOwnership.LIVE


def _abandon(pid: int) -> None:
    """Best-effort cleanup so a failing assertion cannot leak a live child."""
    if _probe_child(pid) is not _ChildOwnership.LIVE:
        # Nothing is running at this pid that this test owns. A pid the probe
        # reaped may already have been recycled onto an unrelated process, so
        # signalling it would kill a stranger rather than clean up after this
        # test.
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    # Someone else may have reaped it first; there is nothing left to collect.
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, 0)


def _wait_until_exited_unreaped(
    pid: int, timeout: float = _EXIT_WINDOW_TIMEOUT
) -> None:
    """Block until *pid* has exited without anything having reaped it.

    A ``kill(pid, 0)`` cannot answer this: the kernel keeps a zombie
    addressable until it is collected, so that probe succeeds for an exited
    child exactly as it does for a running one. Procfs reports the scheduler
    state directly, and a ``Z`` is the window these cases need — the child is
    gone, its status is still collectable, and no reaper has published it.
    Waiting on that fact rather than on elapsed time is what keeps a case
    honest about which state it observed.

    Ends the case through *pytest.skip* where this host has no process table to
    read. Without one there is no way to wait for the window to open, so a case
    that proceeded anyway would signal a child that might still be starting up
    and pass or fail for a reason it does not name. The check is made here
    rather than left to the caller because the wait is the only thing the
    window is observable through, so a caller that reached this point with no
    process table has already lost the case's precondition.

    Raises
    ------
    TimeoutError
        If the child has not exited within *timeout* seconds.
    """
    if not process_state(os.getpid()).available:
        pytest.skip("this host cannot observe an unreaped child's exit")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_state(pid).exited:
            return
        time.sleep(0.01)
    msg = f"child {pid} had not exited within {timeout}s"
    raise TimeoutError(msg)


def _without_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make this host take the collection route a probe-less platform takes.

    The route is selected by a capability the host either has or has not, so a
    host that has it cannot reach the other one by asking nicely. The
    capability itself is what makes the route, so that is what this replaces —
    and in *both* namespaces holding an answer to it:
    :mod:`cuprum._process_exit` reads its own constant in each of the three
    probes, while :mod:`cuprum._wait4_process` reads the copy it imported in
    the delivery guard. Replacing one and leaving the other would leave half
    the stack still probing, which is not a platform that exists.

    Replacing the constant rather than the calls that consult it is what keeps
    the simulation complete as the code changes: a further reader of the
    capability is switched over by this patch whether or not this helper knows
    about it, whereas a list of patched call sites silently starts testing the
    probe-capable route again the moment such a reader appears.
    """
    monkeypatch.setattr(_process_exit, "_NON_REAPING_PROBE_AVAILABLE", False)
    monkeypatch.setattr(_wait4_process, "_NON_REAPING_PROBE_AVAILABLE", False)
