"""The handle's own bookkeeping, driven through process doubles.

The behaviour tests in ``tests/behaviour/test_running_command.py`` drive real
children, which is what makes them evidence about signal return codes and
reaping. This module covers what a real child cannot arrange on demand: a
process that is already gone before a signal is attempted, a platform with no
route to a caller-named signal, and a cancellation timed to land inside the
grace window of a teardown. Each of those is a branch a caller depends on and
none can be reached by asking a real child nicely.

The doubles are deliberately thin. They record what was delivered and expose a
``returncode`` and a ``wait()``, which is the whole surface the handle's
control and settlement paths read; nothing here re-implements a subprocess.
They live in :mod:`cuprum.unittests._running_command_support`, because the
settlement module drives the same doubles and neither module should have to
import the other for them.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

import pytest

from cuprum.sh import (
    SignalAfterExitError,
    TimeoutExpired,
    UnsupportedSignalError,
)
from cuprum.unittests._running_command_support import (
    _EARLY_EXIT,
    _KILLED,
    _TERMINATED,
    _WAITERS,
    _handle,
    _SigtermImmuneProcess,
    _StubProcess,
    _StubRunConfig,
)


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


def test_signal_after_exit_names_the_recorded_code() -> None:
    """Signalling an exited child reports how it ended rather than a bare race.

    A caller coordinating several signals cannot avoid racing the child's own
    exit, so the refusal carries the exit code the handle recorded and the PID
    it belonged to, and nothing is delivered.
    """

    async def run_case() -> None:
        """Attempt a signal after the child has already exited."""
        process = _StubProcess(pid=4323)
        process.exit_with(_EARLY_EXIT)
        handle = _handle(process)

        with pytest.raises(SignalAfterExitError) as raised:
            await handle.signal(signal.SIGTERM)

        assert raised.value.exit_code == _EARLY_EXIT, (
            f"the refusal must carry the child's status {_EARLY_EXIT}, "
            f"got {raised.value.exit_code}"
        )
        assert raised.value.pid == process.pid, (
            "the refusal must name the process it could not reach"
        )
        assert not process.signals, "nothing may be delivered to a gone child"

    asyncio.run(run_case())


def test_signal_is_refused_when_platform_cannot_deliver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform with no route to the signal is named, and nothing is sent.

    The refusal carries both facts a caller needs to choose what to do next —
    the signal that was asked for and the platform that declined — so they
    never have to re-read the handle to find out why it was refused. The
    platform is compared against :data:`sys.platform` rather than merely
    checked for truth: a caller reading a log wants the spelling they would
    grep for, and a bare ``os.name`` reads ``"nt"`` where the documented answer
    is ``"win32"``.
    """

    async def run_case() -> None:
        """Attempt a signal the platform cannot deliver."""
        process = _StubProcess(pid=4324)
        handle = _handle(process)
        monkeypatch.setattr(
            "cuprum._process_handle.control._arbitrary_signals_supported",
            _platform_cannot_deliver,
        )

        with pytest.raises(UnsupportedSignalError) as raised:
            await handle.signal(signal.SIGTERM)

        assert raised.value.signal_number == signal.SIGTERM, (
            "the refusal must name the signal that was asked for"
        )
        assert raised.value.platform_name == sys.platform, (
            "the refusal must name the platform as sys.platform spells it, got "
            f"{raised.value.platform_name!r}"
        )
        assert not process.signals, "an undeliverable signal must not be sent"
        assert process.returncode is None, "the child must be left running"

    asyncio.run(run_case())


def test_platform_refusal_is_reported_ahead_of_the_child_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gone child on a restricted platform reports the platform, not the race.

    Both refusals describe a signal that was not delivered, but only one of
    them is actionable: a caller told the child had exited gives up, while a
    caller told the platform cannot take the request knows to reach for
    ``terminate`` or ``kill`` instead. The platform is therefore checked first,
    and this case is what holds that ordering in place — with a child that has
    already exited, a swap of the two checks would silently report the race.
    """

    async def run_case() -> None:
        """Attempt a signal where both refusals apply."""
        process = _StubProcess(pid=4325)
        process.exit_with(_EARLY_EXIT)
        handle = _handle(process)
        monkeypatch.setattr(
            "cuprum._process_handle.control._arbitrary_signals_supported",
            _platform_cannot_deliver,
        )

        with pytest.raises(UnsupportedSignalError) as raised:
            await handle.signal(signal.SIGTERM)

        assert raised.value.signal_number == signal.SIGTERM, (
            "the platform's refusal must be the one reported"
        )

    asyncio.run(run_case())


def _platform_cannot_deliver() -> bool:
    """Report that this platform cannot deliver a caller-named signal."""
    return False


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
    """

    async def run_case() -> None:
        """Cancel ``kill()`` as it reaps, then inspect what survived."""
        process = _StubProcess()
        handle = _handle(process, _StubRunConfig(grace=0.5))

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

    asyncio.run(run_case())


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
