"""Cancellation landing after the child exits, before its streams are gathered.

A streamed run that owns its group still has work to do once the child has
exited: the idle heartbeat has to be stopped, the stdin writer has to finish,
and the two stream consumers have to be gathered. Each is an await, each can be
cancelled, and each needs the same aftermath — the group is settled and the
consumers are reconciled. The sibling module covers a cancellation that arrives
once the consumers are already being awaited; this one covers the two awaits
that precede the gather.

Those three awaits have to sit inside *one* boundary, and the cleanup that
follows a cancellation inside it has to be *one* shielded unit. Both are claims
about the shape of the code rather than about a schedule, so what these tests
establish is the boundary's extent. A cancellation delivered while the run is
inside it has to leave nothing behind; a cancellation delivered on top of the
cleanup still has to let that cleanup finish.

Arrival at each await is a handshake rather than a wait: the patched call
announces that the run has got there, the test cancels, and only then does the
run continue. The cancellation is therefore delivered to the await under test
however slowly the machine runs, and a run that never reaches it fails loudly
rather than passing on a schedule it did not achieve.

What the doubles replace is only the arrival signal. Everything that decides
what happens after the cancellation — the boundary, the group settlement, the
consumer drain — is the production code, unpatched.
"""

from __future__ import annotations

import asyncio
import sys
import typing as typ

import pytest

from cuprum import _subprocess_stream_run
from cuprum.sh import StdinInput
from cuprum.unittests._post_exit_teardown_support import (
    PARKING_STDIN_BYTES,
    Gate,
    SpawnedWriter,
    TeardownWatch,
    assert_reclaimed,
    await_within,
    held_by,
    owned_builder,
    reach_gate_and_cancel,
    reap_descendant,
    settle,
    start_owned_run,
)
from tests.helpers.timeouts import wait_for_pid_file

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.sh import SafeCmd

# The fixture is imported rather than defined here because every module that
# drives this boundary needs it, and the support module is where its one
# definition lives. Declaring it also records that the import is a name pytest
# resolves at collection time, not an unused one to prune.
__all__ = ["owned_builder"]

_posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups are unavailable on Windows",
)

pytestmark = _posix_only

# The group is the only thing that can reach a descendant a reaped leader left
# holding the pipes, so a cancellation that skips the settlement leaves it
# running. Every assertion that checks for a leak says so in these words.
_LEAKED_ON_CANCEL = (
    "the descendant is still holding the inherited pipes, because nothing "
    "reached the group its child led"
)


def test_a_cancel_at_the_heartbeat_stop_still_settles_the_group(
    owned_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first post-exit await is inside the boundary, so it cannot skip it.

    The child exits, leaving a descendant that holds the run's inherited pipes
    and ignores ``SIGTERM``. Before the consumers can be gathered the run stops
    its idle heartbeat, and a cancellation arriving there has to do everything
    every other exit path does. It is the earliest of the three post-exit
    awaits, so a boundary drawn after it leaves the widest gap: neither the
    group nor the consumers are reached, and the descendant outlives the run
    that spawned it.

    The stop itself is a call that returns at once, so the cancellation is
    placed by holding that call open rather than by hoping to catch it in
    flight. What is under test is where the boundary falls, which is the one
    thing the holding cannot fabricate: a boundary drawn after this call lets
    the cancellation through unhandled, and the descendant below says so.

    The run is given a stdin payload nothing will read, so a writer task is
    outstanding when the cancellation lands and the run has more to reclaim
    than the group. Note what that writer being *finished* afterwards would not
    show: settling the group closes the pipes the writer was parked on, which
    releases it whether or not reconciliation ever reaches it. Only the
    descendant's absence is evidence here; the writer's settlement is pinned
    where no group signal can stand in for reconciliation, in the ``INHERIT``
    arm of the deadline module's parked-writer case.
    """
    pid_file = tmp_path / "post-exit-heartbeat.pid"
    gate = Gate()
    gated = held_by(gate, _subprocess_stream_run._stop_idle_monitor)
    monkeypatch.setattr(_subprocess_stream_run, "_stop_idle_monitor", gated)
    watch = SpawnedWriter(monkeypatch)

    async def run_case() -> None:
        """Cancel at the heartbeat stop with a stdin writer still outstanding."""
        descendant_pid: int | None = None
        run_task = start_owned_run(
            owned_builder,
            pid_file,
            stdin=StdinInput(data=b"x" * PARKING_STDIN_BYTES),
        )
        try:
            descendant_pid = await asyncio.to_thread(
                wait_for_pid_file,
                pid_file,
                context="the descendant a cancelled run must still reach",
            )
            await await_within(gate.reached.wait(), context="the heartbeat stop")
            writer = watch.task
            assert writer is not None, (
                "the run spawned no stdin writer, so this run had nothing "
                "outstanding for its cleanup to reclaim"
            )
            assert not writer.done(), (
                "the stdin writer had already finished before the run was "
                "cancelled, so the cancellation did not land with a run task "
                "outstanding"
            )
            await reach_gate_and_cancel(
                run_task,
                gate,
                context="the heartbeat stop",
            )
            await assert_reclaimed(descendant_pid, _LEAKED_ON_CANCEL)
        finally:
            gate.released.set()
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())


def test_a_cancel_at_the_writer_await_still_settles_the_group(
    owned_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The await on the child's own writer is inside the boundary too.

    The stdin writer is awaited after the exit wait and before the gather, and
    it is what closes the child's stdin. A payload larger than a pipe buffer,
    written to a child that leaves without reading it, keeps that writer
    unfinished, so the run sits on this await with the descendant still holding
    every pipe it inherited.

    A cancellation there is the same class of gap as the heartbeat stop, one
    await later, and the consequence is identical: the group is never signalled
    and the descendant keeps the pipes. The awaiting half is held open so the
    cancellation lands on the await this test names, and the writer is then
    asserted to have been live at that moment — otherwise a run that had
    already cleared the writer could be mistaken for one cancelled at it.

    ``SpawnedWriter`` supplies the handle rather than a local double doing it,
    because it is the same instrumentation the heartbeat case needs and its
    two-module rebinding has one definition to keep in step with the run.
    """
    pid_file = tmp_path / "post-exit-writer.pid"
    gate = Gate()
    watch = SpawnedWriter(monkeypatch)
    real_await_writer = _subprocess_stream_run._await_stdin_writer
    monkeypatch.setattr(
        _subprocess_stream_run,
        "_await_stdin_writer",
        held_by(gate, real_await_writer),
    )

    async def run_case() -> None:
        """Cancel the run while the parked stdin writer is being awaited."""
        descendant_pid: int | None = None
        run_task = start_owned_run(
            owned_builder,
            pid_file,
            stdin=StdinInput(data=b"x" * PARKING_STDIN_BYTES),
        )
        try:
            descendant_pid = await asyncio.to_thread(
                wait_for_pid_file,
                pid_file,
                context="the descendant a cancelled run must still reach",
            )
            assert watch.task is not None, (
                "the run reached the writer await with no writer outstanding, "
                "so the cancellation was not delivered to the await this test "
                "names"
            )
            assert not watch.task.done(), (
                "the writer had already finished when the run was cancelled, so "
                "the cancellation did not land on the await this test names"
            )
            await reach_gate_and_cancel(run_task, gate, context="the writer await")
            await assert_reclaimed(descendant_pid, _LEAKED_ON_CANCEL)
        finally:
            gate.released.set()
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())


def test_a_second_cancel_during_teardown_still_reconciles_the_consumers(
    owned_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teardown and reconciliation are one shielded unit, not two in sequence.

    The first cancellation raises out of the consumer gather and enters the
    post-exit cleanup. That cleanup has two halves — settle the group, then
    reconcile the consumers — and the second is only reachable once the first
    has finished. Shielding them separately does not make them one shielded
    sequence: ``_shielded_cleanup`` finishes the task it was given and then
    re-raises the caller's cancellation, so a *second* cancel arriving during
    teardown ends the helper before reconciliation ever starts.

    The second cancellation models the caller insisting, or a deadline firing
    on top of a cancellation already in flight, and it must not cost the run
    its reconciliation. Establishment of the drain is observed through a
    wrapper around the real one; that the run then *ended* is the other half of
    the evidence, since the shield only returns once what it wraps is done.
    """
    pid_file = tmp_path / "post-exit-second-cancel.pid"
    watch = TeardownWatch(monkeypatch)

    async def run_case() -> None:
        """Cancel twice: once to enter cleanup, once during its first half."""
        descendant_pid: int | None = None
        run_task = start_owned_run(owned_builder, pid_file)
        try:
            descendant_pid = await asyncio.to_thread(
                wait_for_pid_file,
                pid_file,
                context="the descendant a twice-cancelled run must reach",
            )
            # The child has exited and its consumers are being gathered, so
            # the first cancellation can only enter the post-exit cleanup.
            await await_within(
                watch.entered_boundary.wait(), context="the post-exit boundary"
            )
            run_task.cancel()
            await await_within(watch.gate.reached.wait(), context="the group teardown")
            run_task.cancel()  # second cancellation, delivered mid-teardown
            await asyncio.sleep(0)
            watch.gate.released.set()
            await settle(run_task)
            assert watch.drained.is_set(), (
                "a second cancellation arriving during group teardown must not "
                "cost the run its consumer reconciliation: teardown and "
                "reconciliation have to be one shielded unit, or the helper "
                "re-raises out of the first half and the drain is never reached"
            )
            await assert_reclaimed(descendant_pid, _LEAKED_ON_CANCEL)
        finally:
            watch.gate.released.set()
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())
