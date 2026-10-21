"""The shared harness for observing a cancelled run that is streaming stdin.

Two test modules measure what a cancelled streaming run leaves behind, and
they measure the same things at different moments. One cancels against a child
that is still running, on either a parked producer pull or a blocked
``drain()``. The other cancels once the child has exited and the run has
entered the settle window, so the cancellation lands on the settle rather than
on the wait for exit. Both need the same reading — the run's process, the
writer task cuprum created, whether cuprum finalized the caller's producer,
whether the pipe closed, and which tasks survived — so the reading lives here
once and the two modules differ only in *when* they cancel.

Readiness is signalled rather than slept for. Each case parks the writer
through a patched seam and waits on the fact that seam reports, so a
cancellation lands on a writer that is genuinely mid-flight rather than at a
moment the host happened to schedule. The patches record and delegate; every
behaviour under test stays the real one.

The harness deliberately does not own the assertions about *which* stances are
reachable — that belongs to each test module's own docstring, because the two
scenarios are not equally strong and the differences were measured rather than
assumed.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import pathlib
import sys
import typing as typ

from cuprum.sh import ExecutionContext, RunOutputOptions, StdinStream
from tests.helpers.stream_pipes import drain_blocking_payload_size
from tests.helpers.timeouts import pending_tasks

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

    from cuprum._pipeline_types import _StageObservation
    from cuprum._subprocess_spawn import _SubprocessExecution
    from cuprum._subprocess_stdin_stream import _StdinCodec
    from cuprum.sh import CommandResult, SafeCmd


_FINALIZED = "finalized"

# How long a run is given to reach the stance the test asked for. Generous: a
# child interpreter starts first, and this bounds readiness rather than being a
# claim about how long anything should take.
_READINESS_S = 20.0

# How long a cancelled run is given to unwind. Wide enough that a loaded host
# cannot fail it, and finite so a regression fails instead of hanging the suite
# behind pytest's own timeout.
_UNWIND_S = 10.0

# The run's own deadline, deliberately not what ends these runs: it is set high
# so a cancellation is the only thing that can end one, which is what makes the
# assertions about *cancellation* meaningful.
_RUN_DEADLINE_S = 60.0

# Short grace before the child is escalated to SIGKILL, so a teardown that must
# kill a child that ignores SIGTERM still finishes well inside the window.
_CANCEL_GRACE_S = 0.1

# Counting descriptors is a ``/proc`` reading, so it is taken where the kernel
# exposes one and skipped where it does not. The cancellation assertions that
# do not depend on it run everywhere.
_CAN_COUNT_FDS = sys.platform.startswith("linux")


def _open_fd_count() -> int:
    """Count the descriptors this process currently holds open."""
    return sum(1 for _ in pathlib.Path("/proc/self/fd").iterdir())


@dc.dataclass(slots=True)
class _CancellationProbe:
    """The per-run facts a cancelled streaming writer is observed through."""

    docked: asyncio.Event
    draining: asyncio.Event
    settling: asyncio.Event
    finalized: list[str]
    processes: list[asyncio.subprocess.Process]
    writer_task: asyncio.Task[object] | None = None
    # Read by the settle wrapper as the window opens, because it is the only
    # moment that can distinguish an occupied window from an empty one.
    writer_pending_at_settle: bool = False


def _new_probe() -> _CancellationProbe:
    """Build a probe with nothing recorded and every signal cleared."""
    return _CancellationProbe(
        docked=asyncio.Event(),
        draining=asyncio.Event(),
        settling=asyncio.Event(),
        finalized=[],
        processes=[],
    )


def _recording_writer(
    probe: _CancellationProbe,
    real_write: cabc.Callable[..., cabc.Awaitable[None]],
) -> cabc.Callable[..., cabc.Awaitable[None]]:
    """Wrap the streaming writer so a test can see the run's process and task.

    Both scenarios observe the writer the same way, so the recording is built
    once and handed to whichever seam a scenario patches.

    Returns
    -------
    collections.abc.Callable[..., collections.abc.Awaitable[None]]
        A writer that records, then streams exactly as the real one does.
    """

    async def tracked_write(
        process: asyncio.subprocess.Process,
        stream: StdinStream,
        codec: _StdinCodec,
        observation: _StageObservation,
    ) -> None:
        """Record the run's process and writer task, then stream as usual."""
        probe.processes.append(process)
        # The writer runs as its own task, so this is that task's handle: the
        # one cuprum created and is therefore responsible for settling.
        probe.writer_task = asyncio.current_task()
        await real_write(process, stream, codec, observation)

    return tracked_write


def _install_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    block_drain: bool,
) -> _CancellationProbe:
    """Patch the streaming writer so a test can see it and wait on it.

    The streaming writer is patched rather than a payload writer, because the
    producer's finalization is only reachable through the streaming path. The
    patch records and delegates; every behaviour under test stays the real one.

    Patching ``drain`` is process-wide on ``asyncio.StreamWriter``, so it is
    installed for the blocked-write stance alone — the parked-producer stance
    needs no writer-side signal, and a patched ``drain`` there would be an
    unrelated change to the thing being measured.

    Returns
    -------
    _CancellationProbe
        The events, records, and handles the patched writer fills in.
    """
    # Imported lazily: the module is interior, and this file has no business
    # depending on where the streaming writer lives until it patches it.
    import cuprum._subprocess_stdin_stream as stream_module

    probe = _new_probe()
    real_write = stream_module._write_stdin_stream
    real_drain = asyncio.StreamWriter.drain
    monkeypatch.setattr(
        stream_module,
        "_write_stdin_stream",
        _recording_writer(probe, real_write),
    )
    if block_drain:

        async def tracked_drain(self: asyncio.StreamWriter) -> None:
            """Signal that the chunk write has entered ``drain()``, then drain."""
            probe.draining.set()
            await real_drain(self)

        monkeypatch.setattr(asyncio.StreamWriter, "drain", tracked_drain)
    return probe


# How many scheduler turns the settle wrapper gives the writer before it
# reports the window occupied. Only a bound: a writer that has reached its
# blocking write stays there, so further turns cost nothing, and one that is
# already finished ends the loop early. A handful would do; the margin is for a
# loaded host, not because the count is a measurement.
_SETTLE_PUMPS = 100


async def _let_writer_reach_its_park(stdin_task: asyncio.Task[None] | None) -> None:
    """Give the writer the scheduler turns it needs to reach its blocking write."""
    if stdin_task is None:
        return
    for _ in range(_SETTLE_PUMPS):
        await asyncio.sleep(0)
        if stdin_task.done():
            return


def _report_window_opened(
    probe: _CancellationProbe, stdin_task: asyncio.Task[None] | None
) -> None:
    """Record whether the window opened on a live writer, then announce it.

    The reading is taken here rather than left for the test to inspect,
    because once the real wait returns the writer is done either way; only a
    reading taken as the window opens can tell a window that was occupied from
    one that opened on nothing.
    """
    probe.writer_pending_at_settle = stdin_task is not None and not stdin_task.done()
    probe.settling.set()


def _install_post_exit_probe(monkeypatch: pytest.MonkeyPatch) -> _CancellationProbe:
    """Patch the seams that report a run has entered its post-exit wait.

    Two routes reach a child that has already exited, and they do it through
    different names, so one patch cannot cover both. The streamed path settles
    its writer from ``cuprum._subprocess_stream_run._await_stdin_writer``; the
    direct path awaits it under the run's remaining deadline from
    ``cuprum._subprocess_direct_run._await_writer_within_deadline``. The two
    live in different modules because the direct run was split out of the
    streamed one, so each name is patched where it is *resolved* — the module
    that owns the call — rather than at a single shared definition.

    The signal is emitted in both cases from just before the real wait is
    called. That ordering is what makes it usable: setting the event does not
    suspend, so the run reaches its guarded wait in the same scheduler turn
    and is already inside it by the time the waiter it just released runs. A
    signal emitted any earlier would let a cancellation land before the guard
    existed, which is a race the guard is not meant to cover.

    The streamed path's settle is patched where the name is *resolved*:
    ``_await_stdin_writer`` lives in ``_subprocess_stream_post_exit`` and calls
    ``_settle_stdin_writer`` from that module's globals, so that is the binding
    a probe has to replace. The run module no longer resolves it — the post-exit
    phase settles the writer from the post-exit module — so patching the run
    module would only add a name no call path reads.
    ``_line_stream.drain`` resolves ``_settle_stdin_writer`` too, but only for
    ``lines()``, which no caller here uses; patching it would add a binding no
    exercised call path reads.

    Returns
    -------
    _CancellationProbe
        The events, records, and handles the patched seams fill in.
    """
    import cuprum._subprocess_direct_run as direct_run_module
    import cuprum._subprocess_stdin_stream as stream_module
    import cuprum._subprocess_stream_post_exit as post_exit_module
    from cuprum._subprocess_stdin_reclaim import _settle_stdin_writer

    probe = _new_probe()
    real_write = stream_module._write_stdin_stream
    monkeypatch.setattr(
        stream_module,
        "_write_stdin_stream",
        _recording_writer(probe, real_write),
    )

    async def tracked_settle(stdin_task: asyncio.Task[None] | None) -> None:
        """Report the window occupied, then settle for real."""
        await _let_writer_reach_its_park(stdin_task)
        _report_window_opened(probe, stdin_task)
        await _settle_stdin_writer(stdin_task)

    real_await_writer = direct_run_module._await_writer_within_deadline

    async def tracked_await_writer(
        execution: _SubprocessExecution,
        stdin_task: asyncio.Task[None],
    ) -> None:
        """Report the window occupied, then await the writer for real.

        The direct path's post-exit wait is a different shape from the
        streamed settle — it is bounded by the run's own deadline rather than
        by one fixed window — but the moment a cancellation interrupts is the
        same moment, and it is reported the same way so the two arms of a
        parametrized case can be read alike.
        """
        await _let_writer_reach_its_park(stdin_task)
        _report_window_opened(probe, stdin_task)
        await real_await_writer(execution, stdin_task)

    # Both resolution sites. A run takes one path or the other, so only one of
    # the two wrappers is ever entered per test; patching both is what makes
    # the probe independent of which path the case exercises.
    monkeypatch.setattr(post_exit_module, "_settle_stdin_writer", tracked_settle)
    monkeypatch.setattr(
        direct_run_module, "_await_writer_within_deadline", tracked_await_writer
    )
    return probe


@dc.dataclass(frozen=True, slots=True)
class _CancelledRunEvidence:
    """What a cancelled streaming run left behind, read once and recorded.

    Read as one record, straight after the run has ended, so nothing the run
    still legitimately owes is counted as a leak and nothing the caller's own
    failsafe touches can be mistaken for the run's doing.
    """

    finalized: tuple[str, ...]
    child_reaped: bool
    writer_settled: bool
    stdin_settled: bool
    pending: frozenset[asyncio.Task[object]]


def _record_aftermath(probe: _CancellationProbe) -> _CancelledRunEvidence:
    """Read what the run left behind, once it has ended.

    Read in one pass so every field describes the same moment. Recorded in one
    place because both scenarios assert against the same five facts, and a
    field that changed meaning in one of them would otherwise go on meaning the
    old thing in the other.

    Returns
    -------
    _CancelledRunEvidence
        The finalized producer, reaped child, settled writer and pipe, and the
        tasks still pending on the loop.
    """
    process = probe.processes[0]
    return _CancelledRunEvidence(
        finalized=tuple(probe.finalized),
        child_reaped=process.returncode is not None,
        writer_settled=probe.writer_task is not None and probe.writer_task.done(),
        stdin_settled=process.stdin is None or process.stdin.is_closing(),
        pending=frozenset(pending_tasks()),
    )


async def _reclaim_run(task: asyncio.Task[CommandResult]) -> None:
    """Leave no run-owned work behind, whatever happened before this.

    Bounded rather than a bare await: this runs in a ``finally``, so an await
    that outlived a regression would turn a failing test into a hanging suite.
    A task that has already finished is left alone, so a cancellation the
    caller has just observed is not overwritten here.
    """
    if task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
        await asyncio.wait_for(asyncio.shield(task), timeout=_UNWIND_S)


def _settlement_outcome(task: asyncio.Task[CommandResult]) -> str:
    """Describe how a cancelled run settled, without re-raising its outcome.

    Reading the task's result or exception is what keeps this a query: the
    caller learns how the run ended without the ending propagating, so the
    assertion it feeds is reached with a diagnosis instead of being skipped by
    the very failure it exists to report.

    Returns
    -------
    str
        ``"still unwinding"`` if the run outlasted its window,
        ``"cancelled"`` for the expected unwrapped cancellation, and otherwise
        the ending's type and message.
    """
    if not task.done():
        return "still unwinding"
    if task.cancelled():
        return "cancelled"
    error = task.exception()
    if error is None:
        return "completed without raising"
    return f"ended with {type(error).__name__}: {error!s}"


@dc.dataclass(frozen=True, slots=True)
class _StreamingStance:
    """The producer under test and the signal that says it is in place.

    The two travel together because they are one decision: each stance parks
    its producer somewhere specific, and the event is how that same stance
    reports it has arrived. Splitting them across arguments would let a caller
    pair one stance's producer with another's signal and wait forever for a
    state the run never reaches.
    """

    producer: cabc.AsyncIterator[bytes]
    ready: asyncio.Event


async def _cancel_when_ready(
    command: SafeCmd,
    probe: _CancellationProbe,
    stance: _StreamingStance,
    *,
    capture: bool,
) -> _CancelledRunEvidence:
    """Start the run, cancel it once the stance is reached, and record the aftermath.

    Returns
    -------
    _CancelledRunEvidence
        What the cancelled run left behind. The assertions below are what
        report a run that did not propagate ``CancelledError`` unwrapped, or
        that ended before the writer reached the stance under test.
    """
    task = asyncio.create_task(
        command.run(
            stdin=StdinStream(stance.producer),
            output=RunOutputOptions(capture=capture),
            context=ExecutionContext(cancel_grace=_CANCEL_GRACE_S),
            timeout=_RUN_DEADLINE_S,
        )
    )
    try:
        await asyncio.wait_for(stance.ready.wait(), timeout=_READINESS_S)
        assert not task.done(), (
            "the run ended before the writer reached the stance under test, "
            "so this case would not have cancelled a live writer at all"
        )
        # Positive control for the leak census below: the writer task this run
        # created is visible on the loop now, so a census that finds nothing
        # after the cancellation is reporting a real settling rather than
        # counting a loop that had nothing left on it to begin with.
        assert probe.writer_task in pending_tasks(), (
            "the run's stdin writer must be a live task while it streams, or "
            "the post-cancellation census proves nothing"
        )
        task.cancel()
        # Settlement is queried rather than awaited. Awaiting the task would
        # propagate its outcome, forcing this caller to catch it — and a catch
        # wide enough to record "ended some other way" is also wide enough to
        # swallow the very cancellation the assertion below is about.
        await asyncio.wait({task}, timeout=_UNWIND_S)
        outcome = _settlement_outcome(task)
        assert outcome == "cancelled", (
            "cancelling a run that is streaming stdin must propagate "
            "CancelledError unwrapped and promptly; got " + outcome
        )
    finally:
        await _reclaim_run(task)
    return _record_aftermath(probe)


def _assert_teardown_is_complete(evidence: _CancelledRunEvidence) -> None:
    """Assert a cancelled run left no producer, child, writer, or task behind."""
    assert evidence.finalized == (_FINALIZED,), (
        "cuprum owns the producer for the run, so cancelling the run must "
        f"still finalize it; got {evidence.finalized!r}"
    )
    assert evidence.child_reaped, (
        "a cancelled run must terminate and reap its child rather than leave it running"
    )
    assert evidence.writer_settled, (
        "the run's stdin writer task must settle, not survive the cancellation"
    )
    assert evidence.stdin_settled, (
        "the stdin writer must close its pipe, or the child-side end stays "
        "open past the run"
    )
    assert not evidence.pending, (
        "no run-owned task may outlive a cancelled run on the same loop; "
        f"still pending: {sorted(evidence.pending, key=repr)!r}"
    )


# Re-exported so a caller that builds a stance does not have to reach past this
# module for the payload size that stance depends on.
__all__ = [
    "_CANCEL_GRACE_S",
    "_CAN_COUNT_FDS",
    "_FINALIZED",
    "_READINESS_S",
    "_RUN_DEADLINE_S",
    "_UNWIND_S",
    "_CancellationProbe",
    "_CancelledRunEvidence",
    "_StreamingStance",
    "_assert_teardown_is_complete",
    "_cancel_when_ready",
    "_install_post_exit_probe",
    "_install_probe",
    "_open_fd_count",
    "_reclaim_run",
    "_record_aftermath",
    "_settlement_outcome",
    "drain_blocking_payload_size",
]
