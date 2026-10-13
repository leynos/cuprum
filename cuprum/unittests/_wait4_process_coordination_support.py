"""The seams that hold a collection in flight while a delivery arrives.

Two callers can reach a ``wait4``-owned child: the owner's reaping thread, which
collects the status, and any caller delivering a signal. The kernel frees a
child's PID as soon as its status is collected, so the interval between that
collection and the publication of its code is the moment a signal sent by
number can arrive at an unrelated process. This module is what lets a case
stand inside that interval rather than race for it.

Where the kernel can be asked whether the child is still waitable, the window
opens inside the collection itself (:class:`_ParkedCollection`). Where it
cannot, the fallback polls instead, and the window opens between two of its
polls (:class:`_ParkedBetweenPolls`). Both release on their own after
:data:`_OBSERVE_TIMEOUT`, so a failing case cannot hang the suite on a delivery
that never arrives.

The children themselves, the spawn inputs, and the process probes live in
:mod:`cuprum.unittests._wait4_process_support`.
"""

from __future__ import annotations

import os
import signal
import threading
import time
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    from cuprum import _wait4_process

__all__ = [
    "_OBSERVE_TIMEOUT",
    "_WINDOW_OBSERVATION",
    "_ParkedBetweenPolls",
    "_ParkedCollection",
    "_deliver_in_a_thread",
    "_skip_if_uncoordinated_reap",
]

#: How long a case waits for the state it is observing before reporting that it
#: never arrived, and how long it gives a delivery to appear before closing the
#: window that would hide it. Both are generous because neither is a race: the
#: state each case waits for is held by the case itself, so the durations only
#: have to outlast the dispatch that reaches it. A delivery that reads an
#: unestablished state returns in microseconds, so the second is time for one to
#: *appear*, not time for one to finish.
_OBSERVE_TIMEOUT = 5.0
_WINDOW_OBSERVATION = 0.5


def _skip_if_uncoordinated_reap() -> None:
    """End the case as not applicable where a reap cannot be coordinated.

    The coordination rests on the same kernel question the delivery guard asks,
    so the cases asserting it are meaningful only where that question can be
    answered. The platform is asked directly rather than through the constant
    the code under test exports: that constant is part of what these cases
    judge, so reading it here would let a probe switched off in the code turn
    its own failing case into a skip, and the skip would read as a host
    limitation rather than the defect it is.

    Ends the case through *pytest.skip*, which raises rather than returns, so
    there is nothing to describe under a returns heading.
    """
    probe_platform = (
        os.name == "posix" and hasattr(os, "waitid") and hasattr(os, "CLD_EXITED")
    )
    if not probe_platform:
        pytest.skip("this host cannot coordinate a reap against a delivery")


class _ParkedCollection:
    """A ``wait4`` that collects the status, then holds the window open.

    The parking is the point: it lets a case observe the interval in which the
    kernel has taken the child's status but this owner has not yet recorded it,
    without racing the reaper to get there. The call still performs the real
    collection first, so what follows the park is the code under test's own
    publication rather than a stub's.

    Only a call that actually collects parks. A ``WNOHANG`` caller polls until
    the child is collectable, and an early poll that returns ``pid == 0`` has
    taken nothing, so parking on it would open the window before there is any
    status in it — the case would then observe an interval in which the child
    is still reachable and a delivery to it is legitimate. Requiring a real
    collection makes :meth:`wait_until_open` mean what it says.

    The window is closed by :meth:`release`, and it closes on its own after
    :data:`_OBSERVE_TIMEOUT` so a failing case cannot hang the suite on a
    delivery that never arrives.
    """

    def __init__(self) -> None:
        """Start with the window shut and no collection yet observed."""
        self.collected = threading.Event()
        self._release = threading.Event()
        self._real_wait4 = os.wait4

    def __call__(self, pid: int, options: int) -> tuple[int, int, object]:
        """Collect a waitable child's status, then hold the window open."""
        result = self._real_wait4(pid, options)
        if result[0] == 0:
            # An unparked poll: nothing was collected, so there is no window
            # to open and no status for the case to be looking at yet.
            return result
        self.collected.set()
        self._release.wait(_OBSERVE_TIMEOUT)
        return result

    def release(self) -> None:
        """Let the parked collection return, publishing what it collected."""
        self._release.set()

    def wait_until_open(self) -> None:
        """Block until the reaper has collected and parked.

        Ends the case with an ``assert`` rather than a raise, so there is
        nothing to describe under a raises heading: a window that never opens
        means the reaper never reached its collection, which is the case
        failing to establish its own precondition rather than the code under
        test misbehaving.
        """
        assert self.collected.wait(_OBSERVE_TIMEOUT), (
            "the reaper must reach the collection for the window to exist"
        )


class _ParkedBetweenPolls:
    """A ``sleep`` that holds the fallback pump between two of its polls.

    The probe-less route cannot be told when the child becomes collectable, so
    it polls, and the lock must not span the gap between polls: that gap is the
    child's whole life. The gap is the pump's own ``sleep``, the one point in
    the loop where the lock is provably released, so a case opens its window by
    standing in the pump's way there. The pause is therefore the seam a case
    arranges, and it happens between polls rather than inside one. That
    matters: parking inside a poll would mean the lock was already open
    unobserved, and a delivery let through then would say nothing about what
    the lock actually allows.

    :meth:`wait_until_parked` observes the arrival, and :meth:`release` lets
    the pump go on — or, if the case is failing, lets it go on after
    :data:`_OBSERVE_TIMEOUT` so a case cannot hang the suite. It is a context
    manager so the pump is released on every path out, including the paths an
    assertion or an exception takes.

    The release has to follow the observation rather than precede it. A pump
    let go early would collect the child before the delivery reached it, so the
    refusal that followed would look like the deadlock the case is there to
    catch.
    """

    def __init__(self) -> None:
        """Start with the pump unobserved and free to run on."""
        self.parked = threading.Event()
        self._release = threading.Event()
        # The patch that installs this seam reaches the ``time`` module itself,
        # so the real sleep has to be taken before it is installed. Without
        # this, the interval sleep below would call straight back into this
        # method and the pump would recurse instead of waiting.
        self._real_sleep = time.sleep

    def __call__(self, interval: float) -> None:
        """Park the pump, then sleep for the interval it asked for."""
        self.parked.set()
        self._release.wait(_OBSERVE_TIMEOUT)
        self._real_sleep(interval)

    def release(self) -> None:
        """Let the pump move on from the poll it is parked after."""
        self._release.set()

    def wait_until_parked(self) -> bool:
        """Report whether the pump reached the gap before the deadline."""
        return self.parked.wait(_OBSERVE_TIMEOUT)

    def __enter__(self) -> _ParkedBetweenPolls:
        """Offer the seam to a case."""
        return self

    def __exit__(self, *_: object) -> None:
        """Release the pump on every path out of the case."""
        self.release()


def _deliver_in_a_thread(
    process: _wait4_process._Wait4Process,
    observed: dict[str, object],
) -> threading.Thread:
    """Attempt a delivery from another thread, recording what it reported.

    The delivery runs off the event loop and off the reaping thread, which is
    the only position from which the coordination is observable: a delivery
    that shares a thread with the reaper cannot demonstrate that one waits for
    the other. The refusal's own error is recorded rather than raised, because
    the point of the case is what the caller could learn afterwards, not that
    the attempt failed.

    Returns
    -------
    threading.Thread
        The delivery, already started. The caller joins it, because whether it
        finished is itself one of the properties under test.
    """

    def deliver() -> None:
        """Record the refusal and the code visible once it returns."""
        try:
            process.send_signal(signal.SIGTERM)
        except ProcessLookupError as exc:
            observed["error"] = exc
        observed["published"] = process.returncode

    delivery = threading.Thread(target=deliver)
    delivery.start()
    return delivery
