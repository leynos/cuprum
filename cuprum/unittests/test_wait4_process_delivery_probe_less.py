"""The refusals, and the deliveries, of a platform without a probe.

A child that exited on its own, with no wait having touched it, is a zombie
whose status is still collectable — and every non-collecting reading of that
state answers "not collected". A guard built only from those reads finds
nothing to refuse and signals the child, so on this route the guard collects
instead, and the code that collection produces is the one the refusal reports.

The pair of cases here is what keeps that collection honest in both directions.
A delivery to a child that has already ended must refuse with its real status,
and a delivery to a child that is still running must reach it rather than being
refused by a guard that treats "no status collected" as "gone".

What the probe-less route owes while a collection is in flight, and what it
must not wait behind, is in
:mod:`cuprum.unittests.test_wait4_process_delivery_fallback`; the route that can
ask the kernel is in :mod:`cuprum.unittests.test_wait4_process_delivery_guard`.
The children, spawn inputs, and process probes live in
:mod:`cuprum.unittests._wait4_process_support`.
"""

from __future__ import annotations

import asyncio
import signal
import typing as typ

from cuprum.sh import SignalAfterExitError
from cuprum.unittests._wait4_process_coordination_support import _OBSERVE_TIMEOUT
from cuprum.unittests._wait4_process_support import (
    _EXITED,
    _abandon,
    _wait4_only,
    _wait_until_exited_unreaped,
    _without_the_probe,
)
from tests.helpers.catalogue import python_builder

if typ.TYPE_CHECKING:
    import pytest


@_wait4_only
def test_probe_less_delivery_refuses_an_unwaited_exit_with_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe-less platform must refuse with the code the exit actually had.

    This is the case the probe-less route gets wrong unless its guard collects.
    A child that exited on its own, with no wait having touched it, is a zombie
    whose status is still collectable — and every non-collecting reading of
    that state answers "not collected", so a guard built only from those reads
    finds nothing to refuse and signals the zombie. Nothing has collected it,
    so its PID is not freed and no other process can hold it; what the call
    reaches is a process that has already ended, and the handle reports a
    delivery that never happened.

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
