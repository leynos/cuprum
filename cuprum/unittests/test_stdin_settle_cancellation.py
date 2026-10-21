"""Cancelling a run *between* its child's exit and its writer's reclaim.

``cuprum.unittests.test_stdin_stream_cancellation`` cancels against a child
that is still running, so the cancellation lands on the run's wait for exit —
which is guarded, and which reconciles every task it owns before the
cancellation propagates. This module cancels at the one moment that guard does
not cover: after the child has been waited for and reaped, while the run is
inside its settle window waiting on the writer.

That window exists for a writer whose remaining bytes can reach nobody, and for
exactly that reason the timeout it waits out is not a guard against this. A
cancellation arriving during ``asyncio.wait`` interrupts the wait rather than
expiring it, so nothing routes it through the reclaim the expiry path performs.
A run that propagates the cancellation without reclaiming first leaves the
writer task and the caller's parked producer pending on the loop behind it —
invisible to the caller, which has already seen its ``CancelledError``, and
still holding a pipe.

The moment is reached by readiness, not by sleeping. The settle seam is
patched to signal as it is entered and then settle for real, so the
cancellation lands on a run that has demonstrably left the exit wait behind.
The positive control the shared harness asserts is what makes that a fact
rather than a hope: the writer task must still be live at that moment.

The two scenarios are not equally strong, and the differences were measured
rather than assumed. Three separate edits were tried, each restored afterwards,
and each scenario recorded against each:

- Reverting the settle to its shield-only shape — the shape that re-raises the
  caller's cancellation without reclaiming — fails
  ``test_cancelling_after_child_exit_reclaims_the_writer`` and leaves the other
  scenario passing. It is the direction the interruption creates, and the
  failure is the one the deletion predicts: the writer task, the producer, and
  the pipe are all still there when the loop is asked. This is the scenario
  that pins the fix.
- Making the settle unbounded again — the shape the window replaced — fails
  ``test_a_child_that_never_reads_ends_the_run_on_its_deadline`` and leaves the
  other passing. It is the direction the window itself creates, and the
  scenario is the one that pins *it*.
- Deleting the direct path's post-exit wait altogether fails neither. That is
  worth recording rather than hiding, because it is a limit of both scenarios:
  with the wait gone the run returns at once and reports the child's exit code
  rather than its own expiry, and neither scenario cancels on that path, so
  neither has a census that would see it. What covers the direct path's bound
  and its cleanup is
  ``cuprum.unittests.test_run_no_stream_post_exit_deadline``, not this module.

The two scenarios also reach the writer through different names now that the
direct run has been split into its own module: the streamed path settles from
``cuprum._subprocess_stream_run._await_stdin_writer``, the direct path awaits
from ``cuprum._subprocess_direct_run._await_writer_within_deadline``. Only the
first scenario installs the post-exit probe, because only it needs to be told
when the wait has begun; the second case is a deadline case and reaches its
conclusion without instrumenting anything.

So each scenario is the other's negative control as well as its sibling: a
deletion that one repays is measurably invisible to the other, which is what
makes "both pass" a statement about the window rather than about one shared
assertion being agreeable.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum.sh import (
    ExecutionContext,
    RunOutputOptions,
    StdinInput,
    StdinStream,
    TimeoutExpired,
)
from cuprum.unittests._stdin_run_cancellation_support import (
    _CANCEL_GRACE_S,
    _FINALIZED,
    _READINESS_S,
    _RUN_DEADLINE_S,
    _UNWIND_S,
    _assert_teardown_is_complete,
    _install_post_exit_probe,
    _reclaim_run,
    _record_aftermath,
    _settlement_outcome,
)
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import CommandResult, SafeCmd
    from cuprum.unittests._stdin_run_cancellation_support import _CancellationProbe

# How long the grandchild holds the inherited read end open. The test finishes
# long before this, so the window is never the constraint; it is here so a
# grandchild that is never collected still dies on its own. The child cannot be
# asked to reap what it spawned — it exits at once, which is the whole point —
# and the grandchild is reparented to init, so the parent has no handle either.
_GRANDCHILD_LIFETIME_S = 120

# The child hands its own stdin read end to a grandchild and exits at once,
# without reading a byte. The grandchild takes no ``stdin=`` argument, so it
# inherits that descriptor as-is and keeps the read end open past the child's
# exit.
#
# The distinction is what makes the window reachable at all. Once the child is
# reaped, a pipe whose only reader was that child gives the writer ``EPIPE``
# immediately, so the writer settles on its own and the window opens on nothing.
# A read end that outlives the child is the state the window exists for: writes
# still succeed into the kernel's buffer, so there is no error to fail against
# and nothing that will drain them.
_EXITS_AND_HOLDS_STDIN = (
    "import subprocess, sys\n"
    "subprocess.Popen([\n"
    "    sys.executable,\n"
    "    '-c',\n"
    f"    'import time; time.sleep({_GRANDCHILD_LIFETIME_S})',\n"
    "])\n"
)

# Comfortably past the 64 KiB pipe buffer, so the write cannot complete on its
# own: the writer is genuinely mid-transfer when the child goes away, rather
# than having finished before there was anything to reclaim.
_BLOCKING_PAYLOAD_BYTES = 1 << 21

# The deadline the second scenario runs under. It is not what ends that run —
# the settle window is, in well under a second — but it bounds a regression
# that removes the reclaim, so a failure is a failure rather than a hang.
_RECLAIM_DEADLINE_S = 10.0


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter.

    Returns
    -------
    collections.abc.Callable[..., SafeCmd]
        A builder that creates SafeCmd instances for the running interpreter.
    """
    return build_python_builder()


async def _cancel_within_settle(
    command: SafeCmd,
    probe: _CancellationProbe,
    *,
    stdin: StdinStream | StdinInput,
    capture: bool,
) -> str:
    """Cancel the run once its settle begins, and report how it then settled.

    The cancellation is issued from the loop that is still running the run, so
    the aftermath is read while that loop is alive rather than after
    ``asyncio.run`` has closed it — a task left pending at loop close would be
    torn down by the loop itself and the leak would leave no trace to assert
    against.

    Returns
    -------
    str
        The run's settlement outcome, as ``_settlement_outcome`` describes it.
    """
    task: asyncio.Task[CommandResult] = asyncio.create_task(
        command.run(
            stdin=stdin,
            output=RunOutputOptions(capture=capture),
            context=ExecutionContext(cancel_grace=_CANCEL_GRACE_S),
            timeout=_RUN_DEADLINE_S,
        )
    )
    try:
        await asyncio.wait_for(probe.settling.wait(), timeout=_READINESS_S)
        assert not task.done(), (
            "the run ended before its settle began, so this case would not "
            "have cancelled inside the window at all"
        )
        # Positive control, asserted here because this is where a failure is
        # legible: the settle wrapper records whether it caught the writer
        # still pending, and a writer that had already finished would leave
        # nothing for the interruption to strand. Without this the census
        # below could be green about a window that opened on nothing.
        assert probe.writer_pending_at_settle, (
            "the run's stdin writer had already finished when the settle "
            "began, so the window this case interrupts was never occupied"
        )
        task.cancel()
        await asyncio.wait({task}, timeout=_UNWIND_S)
        return _settlement_outcome(task)
    finally:
        await _reclaim_run(task)


def test_cancelling_after_child_exit_reclaims_the_writer(
    python_builder: cabc.Callable[..., SafeCmd],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation inside the settle window still reclaims the writer.

    The child exits at once but leaves a grandchild holding the inherited
    stdin read end, so the producer is parked with nothing that can end it:
    the reclaim is the only thing that will. The cancellation therefore either
    reaches the reclaim or strands the writer, and the pending-task census —
    read while the loop is still alive — reports which. The producer must be
    finalized and the pipe closed as well, because those are what the caller's
    own code and the child-side descriptor depend on; a writer task that merely
    reports ``done`` after being left to the loop would satisfy none of them.

    ``capture`` is enabled so the run takes the streamed path, which is where
    the settle is guarded at its call site. The direct path's own guard is
    covered separately, by the scenario below.
    """
    command = python_builder("-c", _EXITS_AND_HOLDS_STDIN)
    probe = _install_post_exit_probe(monkeypatch)

    async def producer() -> cabc.AsyncIterator[bytes]:
        """Yield one oversized chunk, report the stance, then suspend.

        The chunk is what keeps the writer occupied; the suspension that
        follows is what stops it from being finalized on its own. A producer
        that exhausted would let the writer close the pipe and finish before
        the window ever began.

        Yields
        ------
        bytes
            One chunk, larger than the pipe buffer, so the write cannot
            complete while the child is gone.
        """
        try:
            await asyncio.sleep(0)
            yield b"x" * _BLOCKING_PAYLOAD_BYTES
            probe.docked.set()
            await asyncio.Event().wait()
        finally:
            probe.finalized.append(_FINALIZED)

    async def run_case() -> None:
        """Cancel inside the window and assert what the run left behind."""
        outcome = await _cancel_within_settle(
            command,
            probe,
            stdin=StdinStream(producer()),
            capture=True,
        )
        assert outcome == "cancelled", (
            "cancelling a run that is settling its writer must propagate "
            "CancelledError unwrapped and promptly; got " + outcome
        )
        # Read here, inside the loop, rather than after ``asyncio.run`` has
        # closed it. The loop is what tears down whatever is still pending, so
        # a census taken after it returns could not see a stranded writer even
        # if there was one.
        _assert_teardown_is_complete(_record_aftermath(probe))

    asyncio.run(run_case())


def test_a_child_that_never_reads_ends_the_run_on_its_deadline(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A child that exits without reading cannot hold the run open past its deadline.

    This is the shape the post-exit bound exists for. The child exits at once
    leaving a grandchild holding the read end, the caller supplied more than
    the pipe buffer, and the run captures and echoes nothing — the
    configuration that spawns no stream consumers at all, so there is not even
    a reader to notice the child is gone. The writer blocks in ``drain`` behind
    a payload the dead child will never read.

    The direct path awaits that writer under the run's own remaining deadline,
    not under the streamed path's fixed settle window. That choice is
    deliberate and is what this case records: the direct run has no consumers
    whose output a fixed window would protect, so reclaiming the writer early
    would report the child's success while silently truncating input the
    caller asked cuprum to deliver. Waiting the deadline out and then reporting
    ``TimeoutExpired`` says what actually happened — the input was not
    delivered and the configured budget ran out.

    So the assertion is the deadline, not the child's exit code: a wedged run
    would hang, and a run that quietly abandoned its writer would return ``0``.
    Both are failures here, and they fail differently, which is why the elapsed
    time and the exception are both read.
    """
    command = python_builder("-c", _EXITS_AND_HOLDS_STDIN)

    async def run_case() -> None:
        """Run with more than the pipe can hold and assert the public expiry."""
        with pytest.raises(TimeoutExpired) as raised:
            await command.run(
                stdin=StdinInput(data=b"x" * _BLOCKING_PAYLOAD_BYTES),
                output=RunOutputOptions(capture=False),
                timeout=_RECLAIM_DEADLINE_S,
            )
        assert raised.value.timeout == _RECLAIM_DEADLINE_S, (
            "the expiry must report the timeout the caller configured; a "
            "remaining budget reported as the timeout would make one field "
            f"mean two things; got {raised.value.timeout!r}"
        )

    asyncio.run(run_case())
