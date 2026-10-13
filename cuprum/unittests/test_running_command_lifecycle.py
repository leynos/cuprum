"""The handle's own bookkeeping, driven through process doubles.

The behaviour tests in ``tests/behaviour/test_running_command.py`` drive real
children, which is what makes them evidence about signal return codes and
reaping. This module covers what a real child cannot arrange on demand: a
cancellation timed to land inside the grace window of a teardown, and a stop
repeated by several callers. Each of those is a branch a caller depends on and
none can be reached by asking a real child nicely.

Nothing about time enters any of it. A cancellation timed inside a grace
window and a stop repeated between callers both produce a settled handle, and
each case asserts what the two halves of the pair of stops owed each other
rather than a duration.

The waits a deadline bounds, including the one ``start()`` resolved rather than
the caller, are covered in
:mod:`cuprum.unittests.test_running_command_lifecycle_waits`. What
``signal()`` reports when it cannot deliver is a subject of its own, and it
lives in :mod:`cuprum.unittests.test_running_command_signals`.

The doubles are deliberately thin. They record what was delivered and expose a
``returncode`` and a ``wait()``, which is the whole surface the handle's
control and settlement paths read; nothing here re-implements a subprocess.
They live in :mod:`cuprum.unittests._running_command_support`, because the
settlement modules drive the same doubles and neither module should have to
import the other for them.
"""

from __future__ import annotations

import asyncio
import contextlib
import typing as typ

from cuprum.control_events import ControlOperation, ControlOutcome
from cuprum.control_observation import observe_control
from cuprum.unittests._running_command_support import (
    _KILLED,
    _TERMINATED,
    _WAITERS,
    _handle,
    _SigtermImmuneProcess,
    _StubProcess,
    _StubRunConfig,
)

if typ.TYPE_CHECKING:
    from cuprum.control_events import ControlEvent


def test_repeated_termination_reports_the_one_settlement() -> None:
    """A second ``terminate()`` reports the recorded outcome and sends nothing.

    The child is stopped once. A second stop is a no-op that hands back the
    same ``CommandResult``, so a caller coordinating a stop from two places
    cannot reap the child twice or build a second result that disagrees with
    the first.
    """

    async def run_case() -> None:
        """Terminate twice and compare what each call reported."""
        process = _StubProcess()
        handle = _handle(process)

        first = await handle.terminate()
        second = await handle.terminate()

        assert first is second, (
            "a repeated terminate must return the recorded outcome, not build "
            "a second one"
        )
        assert process.terminate_calls == 1, (
            f"SIGTERM must be delivered once, got {process.terminate_calls}"
        )
        assert process.kill_calls == 0, (
            "a child that died on SIGTERM must not be escalated"
        )
        assert first.exit_code == _TERMINATED, (
            f"a graceful stop must report {_TERMINATED}, got {first.exit_code}"
        )

    asyncio.run(run_case())


def test_repeated_kill_reports_the_one_settlement() -> None:
    """A second ``kill()`` reports the recorded outcome and delivers nothing.

    ``kill`` takes the forced path rather than escalating with a zero grace,
    so the status the caller sees is the one they asked for rather than
    whichever signal happened to end the child.
    """

    async def run_case() -> None:
        """Kill twice and compare what each call reported."""
        process = _StubProcess()
        handle = _handle(process)

        first = await handle.kill()
        second = await handle.kill()

        assert first is second, "a repeated kill must return the recorded outcome"
        assert process.kill_calls == 1, (
            f"SIGKILL must be delivered once, got {process.kill_calls}"
        )
        assert process.terminate_calls == 0, (
            "kill must compel the child rather than ask it first"
        )
        assert first.exit_code == _KILLED, (
            f"a forced stop must report {_KILLED}, got {first.exit_code}"
        )

    asyncio.run(run_case())


def test_concurrent_waits_share_one_settlement() -> None:
    """Several waiters observe the one settlement, none building its own.

    Settlement is a task the handle owns rather than a lock held across the
    wait, which is what lets the waiters share it: the first creates the task,
    the rest find it, and all of them read the same result object.
    """

    async def run_case() -> None:
        """Issue every wait before the child exits, then release it."""
        process = _StubProcess()
        handle = _handle(process)
        waiters = [asyncio.ensure_future(handle.wait()) for _ in range(_WAITERS)]
        # Let every waiter reach the shared settlement before the child exits,
        # so the case exercises concurrent readers rather than a cached result.
        for _ in range(_WAITERS):
            await asyncio.sleep(0)
        assert all(not waiter.done() for waiter in waiters), (
            "every waiter must be pending when the child is still running"
        )
        assert handle.result is None, "a running child has no result to report"

        process.exit_with(0)
        results = await asyncio.gather(*waiters)

        first = results[0]
        assert all(result is first for result in results), (
            "every concurrent wait must observe the one settlement"
        )
        assert first.exit_code == 0, "the shared outcome is the child's own"
        assert handle.result is first, "the settlement publishes what wait returned"

    asyncio.run(run_case())


def test_two_stops_escalate_the_child_once_between_them() -> None:
    """Concurrent stops are serialized, so one child is escalated once.

    The settlement shares the *outcome* of a stop and not the *stop*, so it is
    not enough on its own: two callers entering together would both read a live
    child and both escalate, and a child with its own ``SIGTERM`` handler that
    is slow to die would take the forced signals once per caller. The lock that
    prevents that has to live on the child's own record, because a handle's
    ``terminate`` and a teardown are different routes to the same child and a
    lock each of them held a separate copy of would exclude nothing.

    The child ignores ``SIGTERM``, so the first caller is inside its grace
    window when the second arrives — which is exactly the overlap under test.
    The signal announcement orders the second call against the *opening* of
    that window rather than against a clock, so the case does not depend on how
    the loop happens to schedule the two.
    """

    async def run_case() -> None:
        """Stop one child from two places at once, then count the escalation."""
        process = _SigtermImmuneProcess()
        handle = _handle(process, _StubRunConfig(grace=0.5))

        first = asyncio.ensure_future(handle.terminate())
        await process.signalled.wait()
        # The first caller is now holding the stop across its grace window. A
        # second stop arriving here must wait rather than start an escalation
        # of its own, so the two callers send one SIGTERM and one SIGKILL.
        second = asyncio.ensure_future(handle.terminate())
        results = await asyncio.gather(first, second)

        assert process.terminate_calls == 1, (
            "the child must be asked to stop once between the two callers, got "
            f"{process.terminate_calls} SIGTERMs; a second means the callers "
            "escalated the same child independently"
        )
        assert process.kill_calls == 1, (
            "the escalation must be delivered once between the two callers, "
            f"got {process.kill_calls} SIGKILLs"
        )
        assert [result.exit_code for result in results] == [_KILLED, _KILLED], (
            "both callers must report the one outcome the stop produced, got "
            f"{[result.exit_code for result in results]}"
        )

    asyncio.run(run_case())


def test_teardown_cancelled_mid_grace_still_settles() -> None:
    """A cancellation inside teardown reaps the child *and* records its outcome.

    Stopping and settling are separate steps, and a cancellation landing on the
    first must not skip the second. Reaping without settling is half-ownership:
    the child is gone, but the handle's ``result`` stays ``None`` forever and
    every later caller is told nothing about a child that has already been
    dealt with.

    The child ignores ``SIGTERM``, so the cancellation lands inside the grace
    window while the escalation is still pending. Cancelling there is what
    distinguishes the two behaviours: had the shield not held, the escalation
    would be skipped and a ``SIGTERM``-immune child would outlive its handle.
    """

    async def run_case() -> None:
        """Cancel the block leaving the handle, then inspect what survived."""
        process = _SigtermImmuneProcess()
        # Long enough that the cancellation lands inside the window, short
        # enough that the case does not spend five seconds proving it: the
        # cancellation is timed against ``process.signalled``, not the clock,
        # so it fires as soon as the SIGTERM has been delivered.
        handle = _handle(process, _StubRunConfig(grace=0.5))

        async def leave_context() -> None:
            """Enter the handle's context and leave it at once."""
            async with handle:
                await asyncio.sleep(0)

        task = asyncio.ensure_future(leave_context())
        await process.signalled.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert process.kill_calls == 1, (
            "the escalation must complete before the cancellation propagates, "
            "otherwise a SIGTERM-immune child outlives the handle"
        )
        assert process.returncode == _KILLED, (
            f"the child must be reaped by the escalation, got {process.returncode}"
        )
        result = handle.result
        assert result is not None, (
            "a cancellation during teardown must not leave the handle unsettled"
        )
        assert result.exit_code == _KILLED, (
            "the recorded outcome must report the signal that ended the child, "
            f"got {result.exit_code}"
        )
        assert task.cancelled(), (
            "teardown's failure is the caller's to see when the block succeeded"
        )

    asyncio.run(run_case())


def test_terminate_cancelled_mid_grace_still_settles() -> None:
    """A cancellation inside ``terminate()`` reaps the child *and* settles.

    The public stop operations run the same two steps ``__aexit__`` does, and
    for the same reason: the stop is shielded so the teardown completes, which
    means it re-raises the caller's cancellation only after the child is
    reaped. Settling on the line that follows would never run, leaving a
    handle whose child is gone and whose ``result`` is ``None`` forever.

    This is the public-path counterpart of the block-exit case above. It is
    separate because the two are reached through different methods, and a
    future change that restores the ordering to ``__aexit__`` alone would
    leave this path broken while every block-exit case stayed green.
    """

    async def run_case() -> None:
        """Cancel ``terminate()`` mid-grace and inspect what survived."""
        process = _SigtermImmuneProcess()
        handle = _handle(process, _StubRunConfig(grace=0.5))

        task = asyncio.ensure_future(handle.terminate())
        await process.signalled.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert process.returncode == _KILLED, (
            f"the escalation must still reap the child, got {process.returncode}"
        )
        assert task.cancelled(), (
            "the caller's cancellation must still reach it after teardown"
        )
        result = handle.result
        assert result is not None, (
            "a cancelled terminate() must not leave the handle unsettled; "
            "reaping without settling is the half-owned state it must avoid"
        )
        assert result.exit_code == _KILLED, (
            f"the recorded outcome must report the kill, got {result.exit_code}"
        )

    asyncio.run(run_case())


def test_kill_cancelled_while_reaping_still_settles() -> None:
    """A cancellation inside ``kill()`` also leaves the handle settled.

    ``kill()`` takes the same two-step route as ``terminate()``, so it needs
    the same guarantee. Without it, a forced stop interrupted while the child
    was being reaped would be the one stop that silently handed back a handle
    with no recorded outcome.

    As with ``terminate()`` in
    :mod:`cuprum.unittests.test_running_command_lifecycle_waits`, settling is
    only half of what a caller is owed. The kill reached the child, so the run
    must also say so on the control channel: a handle that records *that* the
    child died but not *who* compelled it loses the distinction the channel
    exists to draw.
    """

    async def run_case() -> None:
        """Cancel ``kill()`` as it reaps, then inspect what survived."""
        process = _StubProcess()
        handle = _handle(process, _StubRunConfig(grace=0.5))
        emitted: list[ControlEvent] = []

        with observe_control(emitted.append):
            task = asyncio.ensure_future(handle.kill())
            # The double announces its own exit, so this waits on the event rather
            # than spinning: a busy loop over ``kill_calls`` would pass by
            # accident whenever the scheduler happened to run the task first,
            # which is exactly the timing this case exists to control.
            await process.exited.wait()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert process.kill_calls == 1, (
            f"the forced stop must be delivered exactly once, got {process.kill_calls}"
        )
        assert task.cancelled(), (
            "the caller's cancellation must still reach it after teardown"
        )
        result = handle.result
        assert result is not None, (
            "a cancelled kill() must not leave the handle unsettled"
        )
        assert result.exit_code == _KILLED, (
            f"the recorded outcome must report the kill, got {result.exit_code}"
        )
        assert [event.operation for event in emitted] == [ControlOperation.KILL], (
            "the completed kill must still be reported on the control channel"
        )
        (event,) = emitted
        assert event.outcome is ControlOutcome.DELIVERED, (
            "a forced stop that reached the child reports delivery, "
            f"got {event.outcome}"
        )

    asyncio.run(run_case())
