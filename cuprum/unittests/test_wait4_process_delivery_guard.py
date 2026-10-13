"""How a delivery to a ``wait4``-owned child is coordinated with its reap.

Two callers can reach the same child here: the owner's reaping thread, which
collects the status, and any caller delivering a signal. The kernel frees a
child's PID as soon as its status is collected, so the interval between the
collection and the publication of its code is the one moment a signal sent by
number can arrive at an unrelated process. Every case in this module is about
closing that interval or about the refusal that replaces it.

Both cases here take the route a host with ``os.waitid`` takes, so each is
marked :data:`~cuprum.unittests._wait4_process_support._wait4_only` and the
window they deliver into is the one :class:`_ParkedCollection` holds open. The
same defect is driven on the route that cannot use the probe, along with the
rest of what that route owes, in
:mod:`cuprum.unittests.test_wait4_process_delivery_fallback`; what it owes
about a child that ended unwatched is in
:mod:`cuprum.unittests.test_wait4_process_delivery_probe_less`.

The children, spawn inputs, and process probes live in
:mod:`cuprum.unittests._wait4_process_support`.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum import _wait4_process
from cuprum.unittests._wait4_process_coordination_support import (
    _OBSERVE_TIMEOUT,
    _WINDOW_OBSERVATION,
    _deliver_in_a_thread,
    _ParkedCollection,
    _skip_if_uncoordinated_reap,
)
from cuprum.unittests._wait4_process_support import (
    _EXITED,
    _exiting_config,
    _wait4_only,
)

if typ.TYPE_CHECKING:
    import threading

    import pytest


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
