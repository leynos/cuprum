"""How a delivery to a ``wait4``-owned child is coordinated with its reap.

Two callers can reach the same child here: the owner's reaping thread, which
collects the status, and any caller delivering a signal. The kernel frees a
child's PID as soon as its status is collected, so the interval between the
collection and the publication of its code is the one moment a signal sent by
number can arrive at an unrelated process. Every case in this module is about
closing that interval or about the refusal that replaces it.

The two routes are covered separately, because the platform chooses between
them and a host can only be one. Where the kernel can be asked whether the
child is still waitable, the guard asks. Where it cannot — macOS on Python
3.12, among others — the guard collects instead, under the same lock the
reaper takes, so the collection that establishes the refusal is also the one
that publishes the code the refusal reports.

:func:`_without_the_probe` in :mod:`cuprum.unittests._wait4_process_support`
turns the second route on for the cases that need it; the rest of the
machinery those cases share lives there too.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import threading
import time

import pytest

from cuprum import _wait4_process
from cuprum.sh import SignalAfterExitError
from cuprum.unittests._wait4_process_support import (
    _EXITED,
    _abandon,
    _exiting_config,
    _sleep_config,
    _wait4_only,
    _wait_until_exited_unreaped,
    _without_the_probe,
)
from tests.helpers.catalogue import python_builder

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


@_wait4_only
def test_send_signal_waits_for_a_collection_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivery arriving during the collection must wait, not slip past.

    This is the defect the coordination closes, driven rather than raced. The
    reaper is parked between the kernel taking the child's status and this
    instance recording it — the interval in which ``returncode`` still reads
    ``None`` for a child that is definitively gone. A delivery checks that
    attribute first, so a delivery that reached the check during the park
    would find nothing to refuse and go on to signal a PID the kernel is free
    to have reused.

    Waiting is what separates the coordinated delivery from the uncoordinated
    one, and it is asserted as a fact about the thread rather than a duration:
    the delivery is still running while the park is held, which no
    lock-free implementation can produce. Once released, the same delivery
    must refuse — the child is gone — so the wait is proven to end in a
    collapse of the window rather than merely an absence.

    A counterfactual confirms the park is load-bearing: with the lock removed
    the delivery completes inside the window instead of waiting, so this case
    fails on the first assertion rather than passing for an unrelated reason.
    """
    _skip_if_uncoordinated_reap()
    parked = _ParkedCollection()
    observed: dict[str, object] = {}

    async def deliver_into_the_window() -> None:
        """Open the window, deliver into it, then let the collection finish."""
        process = await _wait4_process.spawn_wait4_process(_exiting_config())
        assert isinstance(process, _wait4_process._Wait4Process), (
            "the POSIX path must yield the wait4-owning process"
        )
        reaping = asyncio.ensure_future(process.wait())
        # Waiting off the loop is what lets the reaping task run: the park is
        # reached by that task, so blocking the loop here would deadlock the
        # case against itself.
        await asyncio.to_thread(parked.wait_until_open)
        assert process.returncode is None, (
            "the window is open only while the code is unrecorded; a recorded "
            "code means the case is not testing the interval it names"
        )

        delivery = _deliver_in_a_thread(process, observed)
        # Time for an uncoordinated delivery to reach the kill. A coordinated
        # one is still blocked on the lock, so this cannot fail by being early;
        # it can only catch a delivery that entered the window.
        await asyncio.sleep(_WINDOW_OBSERVATION)
        observed["waited"] = delivery.is_alive()
        parked.release()
        delivery.join(_OBSERVE_TIMEOUT)
        assert not delivery.is_alive(), "the delivery must not outlive the window"
        await reaping

    monkeypatch.setattr(_wait4_process.os, "wait4", parked)
    try:
        asyncio.run(deliver_into_the_window())
    finally:
        parked.release()

    assert observed["waited"], (
        "a delivery arriving during the collection must wait for the "
        "publication, but it returned while the window was still open"
    )
    assert isinstance(observed["error"], ProcessLookupError), (
        "the delivery must be refused once the collection is published, "
        f"got {observed['error']!r}"
    )
    assert observed["published"] == _EXITED, (
        "the refusal must read the code the reaper published rather than the "
        f"-1 placeholder, got {observed['published']!r}"
    )


@_wait4_only
def test_send_signal_does_not_wait_on_a_published_child() -> None:
    """Once the code is published, a delivery must be refused at once.

    The coordination must cost a delivery nothing when there is nothing to
    coordinate with. The reaper has already finished here, so the lock is free
    and no park is involved: a delivery that still took the slow path — or
    waited for a reap that had already completed — would show up as a
    delivery that hangs against a child that is definitively gone.

    The refusal is asserted to carry the child's own code as well, because the
    two delivery paths must agree: whether the delivery arrives during the
    collection or long after it, the caller must be told the same thing, and a
    published code is what the second path has to read.

    The delivery is joined within the observation timeout, which is what turns
    "did not wait" into a claim the case can actually make. An unjoined thread
    would leave the assertions reading a record the delivery had not yet
    written, so the case would report the wrong thing under load while still
    appearing to pass. A delivery that genuinely took the slow path here —
    waiting for a reap that had already finished — would hang rather than fail,
    so the join is what keeps the regression inside the case that names it.
    """
    _skip_if_uncoordinated_reap()
    observed: dict[str, object] = {}
    delivery: dict[str, threading.Thread] = {}

    async def refuse_after_the_reap() -> None:
        """Signal a child whose reaper has already published its code."""
        process = await _wait4_process.spawn_wait4_process(_exiting_config())
        assert isinstance(process, _wait4_process._Wait4Process), (
            "the POSIX path must yield the wait4-owning process"
        )
        await process.wait()
        assert process.returncode == _EXITED, (
            "the reaper must have published the code this case delivers after"
        )
        delivery["thread"] = _deliver_in_a_thread(process, observed)

    asyncio.run(refuse_after_the_reap())

    delivery["thread"].join(_OBSERVE_TIMEOUT)
    assert not delivery["thread"].is_alive(), (
        "a delivery to a published child must be refused without waiting for anything"
    )
    assert isinstance(observed["error"], ProcessLookupError), (
        f"a published child must be refused, got {observed['error']!r}"
    )
    assert observed["published"] == _EXITED, (
        f"the refusal must report the child's own code, got {observed['published']!r}"
    )


@_wait4_only
def test_reap_without_a_probe_still_collects_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback must reap and publish exactly as the coordinated route does.

    This is the route a platform without ``os.waitid`` takes — macOS on Python
    3.12 among them, where ``wait4`` exists and the probe does not. It is the
    configuration the coordinated path cannot reach, so the guarantees have to
    be met a second way, and this case is what holds that second way to the
    same promise: one owning reap, the child's own status published, and its
    resource usage attributed to the command that spawned it.

    The child is a live one signalled after the spawn, so the fallback's poll
    is driven through the state it exists for — a running child that is not
    yet collectable — rather than arriving after the child has already gone.
    A fallback that collected a running child's placeholder, or that lost the
    usage by asking without ``WNOHANG`` and blocking, would fail here.
    """
    _without_the_probe(monkeypatch)
    observed: dict[str, object] = {}

    async def reap_through_the_fallback() -> None:
        """Spawn, signal, and let the fallback route reap the child."""
        process = await _wait4_process.spawn_wait4_process(_sleep_config())
        assert isinstance(process, _wait4_process._Wait4Process), (
            "the POSIX path must yield the wait4-owning process"
        )
        try:
            process.send_signal(signal.SIGTERM)
            observed["status"] = await process.wait()
            # Typed as ``int | None`` because the property cannot promise a
            # code before this owner's own reap has published one; the case
            # asserts the published value rather than narrowing it away here.
            observed["published"] = process.returncode
            usage = process.resource_usage
            observed["has_usage"] = int(usage is not None)
        finally:
            _abandon(process.pid)

    asyncio.run(reap_through_the_fallback())

    assert observed["status"] == -signal.SIGTERM, (
        "the fallback must reap the child and report the signal's negation, "
        f"got {observed['status']!r}"
    )
    assert observed["published"] == -signal.SIGTERM, (
        "the fallback must publish the collected code, not a placeholder, got "
        f"{observed['published']!r}"
    )
    assert observed["has_usage"] == 1, (
        "the fallback's collection must still return the child's resource "
        "usage, or the one-reaper guarantee is lost on this platform"
    )


@_wait4_only
def test_fallback_reap_is_coordinated_against_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivery during the fallback's collection must wait, not slip past.

    The probe-less route keeps the lock, so it has to keep what the lock buys:
    a delivery arriving while a collection is in flight waits for the
    publication instead of racing it. This is the same defect the coordinated
    route's own case drives, on the route that cannot use the probe — and it
    is the case that fails if the fallback is ever "simplified" back to an
    uncoordinated collection.

    The window is opened the same way here: the real ``wait4`` collects, then
    parks before returning, so the delivery meets a child whose status the
    kernel has taken but which this instance has not yet recorded.
    """
    _without_the_probe(monkeypatch)
    parked = _ParkedCollection()
    observed: dict[str, object] = {}

    async def deliver_into_the_window() -> None:
        """Open the window, deliver into it, then let the collection finish."""
        process = await _wait4_process.spawn_wait4_process(_exiting_config())
        assert isinstance(process, _wait4_process._Wait4Process), (
            "the POSIX path must yield the wait4-owning process"
        )
        reaping = asyncio.ensure_future(process.wait())
        await asyncio.to_thread(parked.wait_until_open)
        assert process.returncode is None, (
            "the window is open only while the code is unrecorded; a recorded "
            "code means the case is not testing the interval it names"
        )

        delivery = _deliver_in_a_thread(process, observed)
        await asyncio.sleep(_WINDOW_OBSERVATION)
        observed["waited"] = delivery.is_alive()
        parked.release()
        delivery.join(_OBSERVE_TIMEOUT)
        assert not delivery.is_alive(), "the delivery must not outlive the window"
        await reaping

    monkeypatch.setattr(_wait4_process.os, "wait4", parked)
    try:
        asyncio.run(deliver_into_the_window())
    finally:
        parked.release()

    assert observed["waited"], (
        "a delivery arriving during the fallback's collection must wait for "
        "the publication, but it returned while the window was still open"
    )
    assert isinstance(observed["error"], ProcessLookupError), (
        "the delivery must be refused once the collection is published, "
        f"got {observed['error']!r}"
    )
    assert observed["published"] == _EXITED, (
        "the refusal must read the code the reaper published rather than the "
        f"-1 placeholder, got {observed['published']!r}"
    )


@_wait4_only
def test_fallback_delivery_to_a_live_child_does_not_wait_for_the_reap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live child must be signalable while the fallback waits to collect it.

    The probe-less route cannot be told when the child becomes collectable, so
    it polls — and the lock must not span the gap between polls, because that
    gap is the child's whole life. The gap is the pump's own ``sleep``, the one
    point in the loop where the lock is provably released, so the case opens
    its window there: a delivery made while the fallback sleeps between two
    polls must reach the child at once.

    This is the case that fails if the fallback is ever "simplified" into the
    shape the route exists to avoid — a loop holding the lock across its own
    waiting. The child outlives the case by design, so a delivery left waiting
    there would not merely be slow: the sleep that would release it is behind
    the lock the delivery is holding, and the join would time out against a
    deadlock rather than against load.

    A delivery that returned promptly because it refused everything would not
    satisfy the case either. The signal is asserted to have landed, and the
    same reap is asserted to report it — which is what shows the pump and the
    delivery met the same live child rather than two different ones.
    """
    _without_the_probe(monkeypatch)
    between_polls = threading.Event()
    release = threading.Event()
    real_sleep = time.sleep
    observed: dict[str, object] = {}
    delivery: dict[str, threading.Thread] = {}

    def parking_sleep(interval: float) -> None:
        """Hold the pump between two polls, the lock released as it does so."""
        between_polls.set()
        release.wait(_OBSERVE_TIMEOUT)
        real_sleep(interval)

    async def deliver_between_polls() -> None:
        """Signal the child from outside while the fallback sleeps."""
        process = await _wait4_process.spawn_wait4_process(_sleep_config())
        assert isinstance(process, _wait4_process._Wait4Process), (
            "the POSIX path must yield the wait4-owning process"
        )
        reaping = asyncio.ensure_future(process.wait())
        try:
            assert await asyncio.to_thread(between_polls.wait, _OBSERVE_TIMEOUT), (
                "the fallback must let the child run on between its polls"
            )
            delivery["thread"] = _deliver_in_a_thread(process, observed)
            delivery["thread"].join(_OBSERVE_TIMEOUT)
            observed["waited"] = delivery["thread"].is_alive()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(reaping, _OBSERVE_TIMEOUT)
            observed["status"] = process.returncode
        finally:
            release.set()
            _abandon(process.pid)

    monkeypatch.setattr(_wait4_process.time, "sleep", parking_sleep)
    try:
        asyncio.run(deliver_between_polls())
    finally:
        release.set()

    assert not observed["waited"], (
        "a delivery to a live child must not wait on the fallback's reap, but "
        "it was still blocked while the child was alive"
    )
    assert "error" not in observed, (
        "the child was alive when it was signalled, so the delivery must not "
        f"have been refused, got {observed.get('error')!r}"
    )
    assert observed["status"] == -signal.SIGTERM, (
        "the fallback must reap the child the delivery signalled and report "
        f"the signal's negation, got {observed['status']!r}"
    )


@_wait4_only
def test_probe_less_delivery_refuses_an_unwaited_exit_with_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe-less platform must refuse with the code the exit actually had.

    This is the case the probe-less route gets wrong unless its guard collects.
    A child that exited on its own, with no wait having touched it, is a zombie
    whose status is still collectable — and every non-collecting reading of
    that state answers "not collected", so a guard built only from those reads
    finds nothing to refuse and signals a PID the kernel has already freed.

    The refusal is asserted to name the child's own status, which is the point
    of collecting rather than merely checking: a handle that refuses has to
    tell its caller how the child ended, and on this route the collection the
    guard performs is the only reading that can establish it. The status
    travels through the public path a caller reads — the handle refuses with
    the code the owner published — so the assertion is made against
    :class:`~cuprum.sh.SignalAfterExitError` rather than an internal attribute.

    The child is left unwatched before the signal: nothing waits on it, so the
    interval the case names is the real one rather than one this case arranged.
    """
    _without_the_probe(monkeypatch)
    observed: dict[str, object] = {}

    async def refuse_the_unwaited_exit() -> None:
        """Let the child exit on its own, then signal a handle left holding it."""
        command = python_builder()("-c", f"import sys; sys.exit({_EXITED})")
        handle = await command.start()
        try:
            pid = handle.pid
            assert pid is not None, "the child must have been spawned"
            await asyncio.to_thread(_wait_until_exited_unreaped, pid)
            try:
                await handle.signal(signal.SIGTERM)
            except SignalAfterExitError as refusal:
                # Carried out rather than handled: the code the refusal names
                # is the case's subject, and a signal that reached the child
                # would leave no refusal here.
                observed["refusal"] = refusal
        finally:
            await handle.kill()

    asyncio.run(refuse_the_unwaited_exit())

    refusal = observed.get("refusal")
    assert isinstance(refusal, SignalAfterExitError), (
        "a probe-less delivery to a child that exited unwatched must refuse "
        f"with the handle's own error, got {refusal!r}"
    )
    assert refusal.exit_code == _EXITED, (
        "the refusal must name the child's real status, which on this route "
        f"only the guard's own collection can establish; got {refusal.exit_code}"
    )


@_wait4_only
def test_probe_less_delivery_still_reaches_a_live_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collecting guard must not refuse a child that is still running.

    The refusal above is produced by a collection, and a collection on a
    running child collects nothing. This case is what distinguishes that from
    a guard that refuses whenever it looks: the same code path, the same
    patched platform, and a child that is alive — so a guard that treated "no
    status collected" as "gone" would refuse here, and a signal meant for a
    waiting process would never be delivered.

    The child is one that blocks until a signal ends it, and the handle's own
    outcome reports how it went: the negated signal number is what a child
    ended by that signal produces. A guard that refused would leave the child
    running and the outcome unproduceable, so the case cannot pass by refusing.

    The cleanup is deliberately taken at the operating-system level rather
    than through the handle. A guard that refused this signal would also refuse
    the `SIGKILL` a handle-level kill sends, and the handle would then settle
    against a child that is still sleeping — so the regression this case exists
    to catch would stall the suite instead of failing it, and a stall is
    indistinguishable from an unrelated hang. Killing the child directly keeps
    the teardown independent of the behaviour under test.
    """
    _without_the_probe(monkeypatch)
    observed: dict[str, object] = {}

    async def deliver_to_a_live_child() -> None:
        """Signal a child that the collecting guard must find still running."""
        command = python_builder()("-c", "import time; time.sleep(997)")
        handle = await command.start()
        pid = handle.pid
        try:
            await handle.signal(signal.SIGTERM)
            observed["signalled"] = True
            result = await asyncio.wait_for(handle.wait(), _OBSERVE_TIMEOUT)
            observed["result_code"] = result.exit_code
        finally:
            if pid is not None:
                _abandon(pid)

    asyncio.run(deliver_to_a_live_child())

    assert observed.get("signalled") is True, (
        "a live child must be signalled on the probe-less route, not refused"
    )
    assert observed["result_code"] == -signal.SIGTERM, (
        "the delivered signal must be the reason the child ended, so the "
        f"handle reports its negation, got {observed['result_code']!r}"
    )
