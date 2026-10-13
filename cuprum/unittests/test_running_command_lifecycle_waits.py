"""The waits a handle bounds, and the failures a block leaves behind.

``start(timeout=...)`` is the default deadline for every ``wait()`` on that
handle, because the clock a timeout is measured against starts at the wait that
asks for one. That default, the caller's explicit replacement for it, and the
stop an expiry produces are all branches a real child cannot arrange on demand,
so the cases here drive the same doubles as the rest of the handle's unit
tests.

The second subject is what a failing block leaves behind. Teardown does not
suppress the block's own error, keeps the handle settled anyway, and still
propagates a cancellation rather than filing it as one more failure — the three
pulling in different directions.

The handle's repetitions, its shared settlement, and the cancellations that
land inside a stop are covered in
:mod:`cuprum.unittests.test_running_command_lifecycle`; the doubles and the
handle assembly live in :mod:`cuprum.unittests._running_command_support`.
"""

from __future__ import annotations

import asyncio

import pytest

from cuprum.control_events import ControlEvent, ControlOperation, ControlOutcome
from cuprum.control_observation import observe_control
from cuprum.sh import TimeoutExpired
from cuprum.unittests._running_command_support import (
    _TERMINATED,
    _handle,
    _SigtermImmuneProcess,
    _StubProcess,
    _StubRunConfig,
)


def test_teardown_failure_does_not_replace_the_blocks_error() -> None:
    """A block that fails keeps its own error, whatever teardown did.

    Nothing here suppresses: the error that caused the exit is the one the
    caller has to see, so a teardown failure is left on the observation stream
    rather than replacing it. Settlement still happens, because the handle
    that recorded no result would be the harder problem to diagnose.
    """

    async def run_case() -> None:
        """Fail the block while teardown stops the child."""
        process = _SigtermImmuneProcess()
        handle = _handle(process, _StubRunConfig(grace=0.05))
        raised: BaseException | None = None
        try:
            async with handle:
                _raise_block_error()
        except ValueError as failure:
            # Carried out rather than handled: which error reaches the caller
            # is exactly what this case asserts.
            raised = failure

        assert raised is not None, "the block's own error must reach the caller"
        assert str(raised) == "block error", (
            "the block's message must survive teardown intact"
        )
        assert handle.result is not None, (
            "teardown must settle the handle even while the block fails"
        )

    asyncio.run(run_case())


def test_wait_falls_back_to_the_deadline_start_resolved() -> None:
    """A wait with no deadline of its own enforces the one ``start()`` resolved.

    ``start(timeout=...)`` is documented as the default deadline for every
    ``wait()`` on that handle, because the clock a timeout is measured against
    starts at the wait that asks for one. The bundle cannot carry that value —
    the spawn path would act on it — so it travels on the run instead, and this
    case is what holds it there: dropping the field makes every such wait
    unbounded, which no caller can see coming.

    The deadline is already elapsed, so the wait has to raise rather than
    return. A double that never exits is what makes the difference observable:
    an unbounded wait would block until the harness timeout instead.
    """

    async def run_case() -> None:
        """Wait with no argument against an already-elapsed default."""
        process = _StubProcess()
        handle = _handle(process, _StubRunConfig(default_timeout=0))

        with pytest.raises(TimeoutExpired) as raised:
            await handle.wait()

        assert raised.value.timeout == 0, (
            "the expiry must name the deadline start() resolved"
        )
        assert process.terminate_calls == 1, (
            "an expired wait must stop the child it gave up on"
        )

    asyncio.run(run_case())


def test_wait_argument_overrides_the_default_deadline() -> None:
    """An explicit wait deadline replaces the one ``start()`` resolved.

    The two are not combined and the narrower one does not win: the caller who
    names a deadline at the wait has decided when to stop waiting, and the
    standing value is only what serves a caller who names none. A generous
    standing value with an elapsed explicit one therefore has to expire — the
    reverse of the fallback case — which is what distinguishes "override" from
    "whichever is shorter".
    """

    async def run_case() -> None:
        """Name an elapsed deadline where the handle's default is generous."""
        process = _StubProcess()
        handle = _handle(process, _StubRunConfig(default_timeout=30.0))

        with pytest.raises(TimeoutExpired):
            await handle.wait(timeout=0)

    asyncio.run(run_case())


def test_expired_wait_records_the_outcome_it_stopped() -> None:
    """The child an expired deadline stopped is still reported afterwards.

    A wait whose clock runs out stops the child the way a cancelled ``run()``
    does, so by the time ``TimeoutExpired`` reaches the caller the child is
    already reaped and has a real exit code. Discarding it would leave the
    handle claiming ``result is None`` about a child it had just dealt with,
    and the only way to learn what happened would be to re-read the process.

    The deadline is what raises, and it does so exactly once: an implementation
    that lets the settlement task fail instead re-raises at every later
    operation, so each of the three assertions below is a separate witness to
    the same defect.
    """

    async def run_case() -> None:
        """Expire a wait, then read the outcome three ways."""
        process = _StubProcess()
        handle = _handle(process, _StubRunConfig(default_timeout=0))

        with pytest.raises(TimeoutExpired):
            await handle.wait()

        recorded = handle.result
        assert recorded is not None, (
            "the stopped child's outcome must be recorded, not discarded"
        )
        assert recorded.exit_code == _TERMINATED, (
            f"the recorded outcome must report the stop, got {recorded.exit_code}"
        )

        repeated = await handle.wait()
        assert repeated is recorded, (
            "a later wait must report the recorded outcome rather than "
            "re-raising the deadline that is already spent"
        )

        terminated = await handle.terminate()
        assert terminated is recorded, (
            "a stop after an expired deadline must report the recorded "
            "outcome, as its contract promises"
        )
        assert process.terminate_calls == 1, (
            f"the child must be stopped once, got {process.terminate_calls}"
        )

    asyncio.run(run_case())


def test_expired_wait_does_not_displace_the_outcome_for_a_later_kill() -> None:
    """A kill after an expired deadline reports the outcome, not the expiry.

    ``terminate`` and ``kill`` reach the recorded outcome by different routes —
    one stops the child first, the other compels it — so both are asserted. A
    kill is also the case where a resurrected deadline error would be most
    misleading: it would report a timeout in answer to a caller who asked for a
    forced stop.
    """

    async def run_case() -> None:
        """Expire a wait, then kill the handle."""
        process = _StubProcess()
        handle = _handle(process, _StubRunConfig(default_timeout=0))

        with pytest.raises(TimeoutExpired):
            await handle.wait()

        recorded = handle.result
        killed = await handle.kill()

        assert killed is recorded, (
            "a kill must report the recorded outcome after an expired deadline"
        )
        assert killed.exit_code == _TERMINATED, (
            f"the child was already stopped, so the recorded code stands, "
            f"got {killed.exit_code}"
        )

    asyncio.run(run_case())


def test_teardown_after_an_expired_wait_keeps_the_recorded_outcome() -> None:
    """Leaving the ``async with`` block after a deadline keeps the outcome.

    Teardown is the last chance to settle a handle, and the case that would
    otherwise slip through is a block that fails while its wait had already
    expired: the deadline error must not be the thing teardown reports, and the
    outcome it recorded must still be readable.
    """

    async def run_case() -> None:
        """Expire a wait inside a block that then exits normally."""
        process = _StubProcess()
        handle = _handle(process, _StubRunConfig(default_timeout=0))

        async with handle:
            with pytest.raises(TimeoutExpired):
                await handle.wait()

        assert handle.result is not None, (
            "teardown must leave the recorded outcome readable"
        )
        assert handle.result.exit_code == _TERMINATED, (
            f"the recorded outcome must be the stop's, got {handle.result.exit_code}"
        )

    asyncio.run(run_case())


def test_cancellation_survives_a_failing_block() -> None:
    """A cancelled teardown propagates even when the block already failed.

    The two rules pull in opposite directions and only one of them can win.
    A block that raises keeps its own error — teardown does not get to replace
    it — but a cancellation is not a teardown *failure*, it is a request that
    the task stop, and ``_shielded_cleanup`` re-raises it precisely so it is
    not lost. Holding that cancellation as an ordinary failure and dropping it
    because the block failed is what loses it: the caller handles ``ValueError``
    and carries on, while the task stays marked cancelled with no matching
    ``CancelledError`` — so an enclosing ``asyncio.timeout`` or task-group
    shutdown waits on a task that was never actually cancelled.

    What the caller sees is therefore both facts at once: the cancellation
    wins, and the task *ends* as cancelled rather than merely being marked so —
    which is the difference between a request that was delivered and one that
    was only counted.
    """

    async def run_case() -> None:
        """Fail the block, cancel inside the grace, and inspect the task."""
        process = _SigtermImmuneProcess()
        handle = _handle(process, _StubRunConfig(grace=0.5))

        async def fail_and_leave() -> None:
            """Fail the block, then leave the context while teardown runs."""
            async with handle:
                _raise_block_error()

        task = asyncio.ensure_future(fail_and_leave())
        # Let the block fail and teardown reach the point where it is waiting
        # out the grace period, so the cancellation lands inside it rather than
        # before the block has even started.
        await process.signalled.wait()
        task.cancel()
        raised: BaseException | None = None
        try:
            await task
        except BaseException as failure:  # ruff: ignore[blind-except] - carried out: which error reaches the caller is what this case asserts
            raised = failure

        assert isinstance(raised, asyncio.CancelledError), (
            "the cancellation must reach the caller rather than being "
            f"swallowed by the block's error; got {raised!r}"
        )
        assert task.cancelled(), (
            "the task must end as cancelled rather than merely counted as "
            "such, which is what a swallowed request leaves behind"
        )
        assert process.kill_calls == 1, (
            "the escalation must still complete before the cancellation "
            f"propagates, got {process.kill_calls} kills"
        )
        assert handle.result is not None, (
            "a cancelled teardown must still settle the handle"
        )

    asyncio.run(run_case())


def test_a_cancelled_stop_still_reports_what_it_did() -> None:
    """A stop cancelled inside its grace still emits the control event it earned.

    The stop reached the child, so an operator reading the run afterwards is
    owed the fact. Emitting that event is driven by the value the shielded step
    produced, and dropping the value whenever the caller was cancelled lost it:
    the child was left stopped with nothing recorded saying who stopped it, and
    the refusal-versus-escalation distinction this channel exists to draw went
    with it.

    The cancellation is still the caller's own to see. The claim under test is
    narrower than "the stop succeeded" — it is that a cancellation *around* a
    completed stop does not erase the stop's record.
    """

    async def run_case() -> None:
        """Cancel a stop mid-grace and inspect the emitted control events."""
        process = _SigtermImmuneProcess()
        handle = _handle(process, _StubRunConfig(grace=0.5))
        emitted: list[ControlEvent] = []
        with observe_control(emitted.append):
            stopping = asyncio.ensure_future(handle.terminate())
            # Wait for the grace window itself rather than a sleep, so the
            # cancellation lands inside the stop rather than before it started.
            await process.signalled.wait()
            stopping.cancel()
            raised: BaseException | None = None
            try:
                await stopping
            except BaseException as failure:  # ruff: ignore[blind-except] - carried out: which error reaches the caller is what this case asserts
                raised = failure

        assert isinstance(raised, asyncio.CancelledError), (
            f"the cancellation must reach the caller; got {raised!r}"
        )
        assert process.kill_calls == 1, (
            "the escalation must complete before the cancellation propagates, "
            f"got {process.kill_calls} kills"
        )
        assert [event.operation for event in emitted] == [ControlOperation.TERMINATE], (
            "the completed stop must still be reported on the control channel"
        )
        (event,) = emitted
        assert event.outcome is ControlOutcome.ESCALATED, (
            "a stop that had to compel the child reports the escalation, "
            f"got {event.outcome}"
        )

    asyncio.run(run_case())


def _raise_block_error() -> None:
    """Raise the error a block fails with.

    Raised from a helper so the ``raise`` does not sit inside the ``try`` whose
    handler the case is testing.

    Raises
    ------
    ValueError
        Always, carrying the message the case asserts survives teardown.
    """
    msg = "block error"
    raise ValueError(msg)
