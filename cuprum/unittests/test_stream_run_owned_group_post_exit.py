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
import time
import typing as typ

import pytest

from cuprum import _subprocess_stream_run
from cuprum.sh import (
    ExecutionContext,
    ProcessGroupPolicy,
    RunOutputOptions,
    StdinInput,
)
from cuprum.unittests._post_exit_teardown_support import (
    PARKING_STDIN_BYTES,
    RUN_BUDGET_S,
    Gate,
    TeardownWatch,
    assert_reclaimed,
    await_within,
    builder,
    held_by,
    owned_scope,
    reach_gate_and_cancel,
    reap_descendant,
    recorded_descendant,
    settle,
    start_owned_run,
)
from tests.helpers.timeouts import (
    descendant_then_exit_argv,
    wait_for_pid_file,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum._pipeline_types import _StageObservation
    from cuprum.sh import SafeCmd

# The two fixtures are imported rather than defined here because both modules
# that drive this boundary need them, and the support module is where their one
# definition lives. Declaring them also records that the imports are names
# pytest resolves at collection time, not unused ones to prune.
__all__ = ["builder", "owned_scope"]

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
    builder: cabc.Callable[..., SafeCmd],
    owned_scope: None,
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
    """
    pid_file = tmp_path / "post-exit-heartbeat.pid"
    gate = Gate()
    gated = held_by(gate, _subprocess_stream_run._stop_idle_monitor)
    monkeypatch.setattr(_subprocess_stream_run, "_stop_idle_monitor", gated)

    async def run_case() -> None:
        """Cancel the run while it is at the heartbeat stop."""
        descendant_pid: int | None = None
        run_task = start_owned_run(builder, pid_file)
        try:
            descendant_pid = await asyncio.to_thread(
                wait_for_pid_file,
                pid_file,
                context="the descendant a cancelled run must still reach",
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
    builder: cabc.Callable[..., SafeCmd],
    owned_scope: None,
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
    """
    pid_file = tmp_path / "post-exit-writer.pid"
    gate = Gate()
    spawned: list[asyncio.Task[None] | None] = []
    real_spawn = _subprocess_stream_run._spawn_stdin_writer
    real_await_writer = _subprocess_stream_run._await_stdin_writer

    def watching_spawn(
        process: asyncio.subprocess.Process,
        stdin_data: bytes | None,
        observation: _StageObservation,
    ) -> asyncio.Task[None] | None:
        """Spawn the writer unchanged, keeping a handle on the task it made."""
        task = real_spawn(process, stdin_data, observation)
        spawned.append(task)
        return task

    monkeypatch.setattr(_subprocess_stream_run, "_spawn_stdin_writer", watching_spawn)
    monkeypatch.setattr(
        _subprocess_stream_run,
        "_await_stdin_writer",
        held_by(gate, real_await_writer),
    )

    async def run_case() -> None:
        """Cancel the run while the parked stdin writer is being awaited."""
        descendant_pid: int | None = None
        run_task = start_owned_run(
            builder,
            pid_file,
            stdin=StdinInput(data=b"x" * PARKING_STDIN_BYTES),
        )
        try:
            descendant_pid = await asyncio.to_thread(
                wait_for_pid_file,
                pid_file,
                context="the descendant a cancelled run must still reach",
            )
            writer_task = spawned[0] if spawned else None
            assert writer_task is not None, (
                "the run reached the writer await with no writer outstanding, "
                "so the cancellation was not delivered to the await this test "
                "names"
            )
            assert not writer_task.done(), (
                "the writer had already finished when the run was cancelled, so "
                "the cancellation did not land on the await this test names"
            )
            await reach_gate_and_cancel(run_task, gate, context="the writer await")
            await assert_reclaimed(descendant_pid, _LEAKED_ON_CANCEL)
        finally:
            gate.released.set()
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())


def test_a_descendant_holding_the_pipes_does_not_outlive_the_deadline(
    builder: cabc.Callable[..., SafeCmd],
    owned_scope: None,
    tmp_path: Path,
) -> None:
    """A configured timeout covers the stream collection, not just the exit wait.

    The child exits well before the deadline, so the exit wait succeeds and the
    run moves on to collecting its consumers — where a descendant it left
    behind holds the inherited pipes, and the readers therefore never see
    end-of-file. Nothing in that second half used to look at the deadline at
    all, so the timeout measured from the run's start silently covered only its
    first half and the run hung indefinitely: the descendant was never
    signalled, because the cleanup that would signal it is what the hang
    prevented.

    The deadline now bounds the collection as well, and expiry takes the
    ordinary cleanup route — so this also asserts the descendant is gone. A fix
    that merely stopped waiting, without settling the group, would return on
    time and leave the descendant holding the pipes, which is the failure the
    policy exists to prevent.
    """
    pid_file = tmp_path / "post-exit-deadline.pid"
    command = builder(*descendant_then_exit_argv(pid_file, exit_code=0))
    deadline = 1.0

    async def run_case() -> None:
        """Run with a deadline the child meets but the collection cannot."""
        descendant_pid: int | None = None
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    command.run(
                        output=RunOutputOptions(capture=True, echo=False),
                        timeout=deadline,
                        context=ExecutionContext(
                            process_group=ProcessGroupPolicy.OWN_GROUP,
                        ),
                    ),
                    RUN_BUDGET_S,
                )
            elapsed = time.monotonic() - started
            assert elapsed < RUN_BUDGET_S, (
                "the run reached this assertion too late to have been bounded "
                "by its own deadline"
            )
            descendant_pid = recorded_descendant(pid_file)
            assert descendant_pid is not None, (
                "the child never recorded the descendant it left behind, so "
                "this run did not reach the state under test"
            )
            await assert_reclaimed(
                descendant_pid,
                "a deadline expiring during stream collection must settle the "
                "group its child led, not merely stop waiting on consumers "
                "that can never see end-of-file",
            )
        finally:
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())


def test_a_second_cancel_during_teardown_still_reconciles_the_consumers(
    builder: cabc.Callable[..., SafeCmd],
    owned_scope: None,
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
        run_task = start_owned_run(builder, pid_file)
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
