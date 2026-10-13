"""What the probe-less reap owes, and what it must never wait behind.

Where the kernel cannot be asked whether a child is still waitable, the guard
collects instead, under the same lock the reaper takes, so the collection that
establishes a refusal is also the one that publishes the code it reports. That
route is what these cases drive: :func:`_without_the_probe` in
:mod:`cuprum.unittests._wait4_process_support` turns it on, because the route
is selected by a capability the host either has or has not.

Three promises are asserted here. A delivery arriving while the fallback's
collection is in flight waits for the publication rather than racing it. A
delivery to a child that is still alive does not wait on the reap at all — the
lock must not span the pump's own waiting, which is the shape the fallback
exists to avoid. And the fallback's reap still reports one owning collection,
the child's own status, and its resource usage.

What the probe-less route owes a child that has already ended, and what it must
not do to one that is still running, is in
:mod:`cuprum.unittests.test_wait4_process_delivery_probe_less`; the coordinated
route's own window is in
:mod:`cuprum.unittests.test_wait4_process_delivery_guard`.
"""

from __future__ import annotations

import asyncio
import signal
import typing as typ

from cuprum import _wait4_process
from cuprum.unittests._wait4_process_coordination_support import (
    _OBSERVE_TIMEOUT,
    _WINDOW_OBSERVATION,
    _deliver_in_a_thread,
    _ParkedBetweenPolls,
    _ParkedCollection,
)
from cuprum.unittests._wait4_process_support import (
    _EXITED,
    _abandon,
    _exiting_config,
    _sleep_config,
    _wait4_only,
    _without_the_probe,
)

if typ.TYPE_CHECKING:
    import threading

    import pytest


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

    The park is released once the delivery has been observed, and its own
    expiry exists only so a failing case cannot hang the suite. Leaving the
    pump to time out on its own would put two independent deadlines on the same
    five seconds — the park's and the await below — and the case would then
    pass or fail on which of them happened to fire first.
    """
    _without_the_probe(monkeypatch)
    observed: dict[str, object] = {}
    delivery: dict[str, threading.Thread] = {}

    async def deliver_between_polls() -> None:
        """Signal the child from outside while the pump is parked between polls."""
        process = await _wait4_process.spawn_wait4_process(_sleep_config())
        assert isinstance(process, _wait4_process._Wait4Process), (
            "the POSIX path must yield the wait4-owning process"
        )
        reaping = asyncio.ensure_future(process.wait())
        try:
            assert await asyncio.to_thread(park.wait_until_parked), (
                "the fallback must let the child run on between its polls"
            )
            delivery["thread"] = _deliver_in_a_thread(process, observed)
            delivery["thread"].join(_OBSERVE_TIMEOUT)
            observed["waited"] = delivery["thread"].is_alive()
            # Bounded rather than bare: a reap that never completes is this
            # case's own failure and should be reported as one, not left to
            # the suite's global timeout to name.
            park.release()
            await asyncio.wait_for(reaping, _OBSERVE_TIMEOUT)
            observed["status"] = process.returncode
        finally:
            _abandon(process.pid)

    with _ParkedBetweenPolls() as park:
        monkeypatch.setattr(_wait4_process.time, "sleep", park)
        asyncio.run(deliver_between_polls())

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
