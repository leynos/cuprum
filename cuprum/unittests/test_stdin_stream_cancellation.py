"""Cancelling a run that is streaming stdin, and what its teardown leaves.

``StdinStream`` hands a run two things it must reclaim: a producer that may be
parked in its next pull, and a writer that may be blocked in ``drain()``. A
cancellation has to reach both, and it is control flow rather than a source
failure, so it must arrive at the caller as ``CancelledError`` rather than
wrapped in a ``StdinSourceError``.

Readiness is signalled rather than slept for. Each case parks the writer
through a patched seam and waits on the fact that seam reports — the producer
docked in its next pull, or the chunk write's ``drain()`` entered — so the
cancellation lands on a writer that is genuinely mid-flight rather than at a
moment the host happened to schedule. Both stances matter: a parked producer
is suspended in the run's *pull*, a blocked ``drain()`` in its *write*, and
those are different awaits for the teardown to reach.

The cases are not equally strong, and the differences were measured rather
than assumed. Two separate single-line deletions were tried, each restored
afterwards, and each case was recorded as failing or surviving:

- Deleting cuprum's ``aclose`` of the producer failed every blocked-``drain()``
  case and survived the parked-pull ones. A producer suspended at its ``yield``
  has no await left to receive the cancellation, so its ``finally`` runs only
  because cuprum closes it; a producer suspended in its own pull unwinds as a
  consequence of the cancellation itself. The blocked-``drain()`` pair is
  therefore what pins cuprum's own cleanup rather than the cancellation's.
- Deleting the cancel-path reclaim of the writer task failed
  ``capture-on``/``parked-pull``, repeatably across three runs, and survived
  the other three. So that reclaim is load-bearing for one combination here,
  and the assertion still has to be made for all four.

The point of asserting every combination is that neither deletion alone would
have been detected by all of them. A suite that only exercised the stance that
happens to unwind by itself would have recorded a green result for both.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import pathlib
import sys
import typing as typ

import pytest

from cuprum.sh import ExecutionContext, RunOutputOptions, StdinStream
from tests.helpers.catalogue import python_builder as build_python_builder
from tests.helpers.stream_pipes import drain_blocking_payload_size
from tests.helpers.timeouts import pending_tasks

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import _StageObservation
    from cuprum._subprocess_stdin_stream import _StdinCodec
    from cuprum.sh import CommandResult, SafeCmd

# The child blocks far longer than any window here, so a run that has finished
# can only have finished because it was torn down, never because the child
# exited of its own accord.
_NEVER_READS = "import time; time.sleep(30)"

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


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter.

    Returns
    -------
    collections.abc.Callable[..., SafeCmd]
        A builder that creates SafeCmd instances for the running interpreter.
    """
    return build_python_builder()


@dc.dataclass(slots=True)
class _CancellationProbe:
    """The per-run facts a cancelled streaming writer is observed through."""

    docked: asyncio.Event
    draining: asyncio.Event
    finalized: list[str]
    processes: list[asyncio.subprocess.Process]
    writer_task: asyncio.Task[object] | None = None


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

    probe = _CancellationProbe(
        docked=asyncio.Event(),
        draining=asyncio.Event(),
        finalized=[],
        processes=[],
    )
    real_write = stream_module._write_stdin_stream
    real_drain = asyncio.StreamWriter.drain

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

    async def tracked_drain(self: asyncio.StreamWriter) -> None:
        """Signal that the chunk write has entered ``drain()``, then drain."""
        probe.draining.set()
        await real_drain(self)

    monkeypatch.setattr(stream_module, "_write_stdin_stream", tracked_write)
    if block_drain:
        monkeypatch.setattr(asyncio.StreamWriter, "drain", tracked_drain)
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


async def _cancel_when_ready(
    command: SafeCmd,
    probe: _CancellationProbe,
    producer: cabc.AsyncIterator[bytes],
    *,
    capture: bool,
    ready: asyncio.Event,
) -> _CancelledRunEvidence:
    """Start the run, cancel it once *ready* fires, and record the aftermath.

    Returns
    -------
    _CancelledRunEvidence
        What the cancelled run left behind. The assertions below are what
        report a run that did not propagate ``CancelledError`` unwrapped, or
        that ended before the writer reached the stance under test.
    """
    task = asyncio.create_task(
        command.run(
            stdin=StdinStream(producer),
            output=RunOutputOptions(capture=capture),
            context=ExecutionContext(cancel_grace=_CANCEL_GRACE_S),
            timeout=_RUN_DEADLINE_S,
        )
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=_READINESS_S)
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
    process = probe.processes[0]
    return _CancelledRunEvidence(
        finalized=tuple(probe.finalized),
        child_reaped=process.returncode is not None,
        writer_settled=probe.writer_task is not None and probe.writer_task.done(),
        stdin_settled=process.stdin is None or process.stdin.is_closing(),
        pending=frozenset(pending_tasks()),
    )


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


@pytest.mark.parametrize(
    "block_drain", [False, True], ids=["parked-pull", "blocked-drain"]
)
@pytest.mark.parametrize("capture", [False, True], ids=["capture-off", "capture-on"])
def test_cancelling_a_streaming_run_tears_everything_down(
    capture: bool,
    block_drain: bool,
    python_builder: cabc.Callable[..., SafeCmd],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation reaches both awaits and leaves nothing behind.

    The producer parks after its first chunk, so the cancellation lands either
    on the run's next pull or on the write of the chunk it already has. Either
    way the producer is finalized, the child is reaped, the writer settles, the
    pipe closes, and no task survives the run — on both the capture-enabled and
    capture-disabled paths, which reconcile their work differently.

    The descriptor count is compared only where a ``/proc`` census is
    available; every other assertion is portable and runs everywhere.
    """
    command = python_builder("-c", _NEVER_READS)
    probe = _install_probe(monkeypatch, block_drain=block_drain)
    open_fds_before = _open_fd_count() if _CAN_COUNT_FDS else None

    async def producer() -> cabc.AsyncIterator[bytes]:
        """Yield one chunk, report the stance, then suspend indefinitely.

        Suspending after the first chunk is the point: it is what leaves the
        writer with nothing to fail against, so only the run's teardown can
        end this producer.

        Yields
        ------
        bytes
            One chunk, large enough to block the writer's ``drain()`` when the
            blocked-write stance asked for that.
        """
        try:
            await asyncio.sleep(0)
            if block_drain:
                # Oversized, so the writer's ``drain()`` cannot complete and
                # the signal below means a genuinely blocked write.
                yield b"x" * drain_blocking_payload_size()
            else:
                yield b"first\n"
            probe.docked.set()
            await asyncio.Event().wait()
        finally:
            probe.finalized.append(_FINALIZED)

    ready = probe.draining if block_drain else probe.docked
    evidence = asyncio.run(
        _cancel_when_ready(
            command,
            probe,
            producer(),
            capture=capture,
            ready=ready,
        )
    )

    _assert_teardown_is_complete(evidence)
    if open_fds_before is not None:
        open_fds_after = _open_fd_count()
        assert open_fds_after == open_fds_before, (
            "a cancelled run must release every descriptor it opened; "
            f"{open_fds_before} were open before it and {open_fds_after} after"
        )
