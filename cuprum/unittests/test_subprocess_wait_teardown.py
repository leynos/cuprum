"""Wait and teardown semantics for the subprocess timeout path.

``test_subprocess_timeout`` covers the drain and the timeout payload; these
tests cover the wait itself. ``_wait_for_exit_code`` has to terminate the
process on deadline expiry and on cancellation, and a cancellation that lands
inside the grace period must still complete the SIGKILL escalation — a
``SIGTERM``-immune child would otherwise outlive the run that returned. The
process doubles those cases share live here with them.
"""

from __future__ import annotations

import asyncio
import contextlib
import typing as typ

import pytest

from cuprum._subprocess_wait import (
    _deadline_for_collection,
    _wait_for_exit_code,
    _wait_for_exit_code_within_timeout,
)
from cuprum.sh import ExecutionContext
from cuprum.unittests._timeout_test_helpers import (
    _ClockObservation,
    _DeadlineExecution,
    _ExitedProcess,
    _TimeoutWaitProcess,
)

if typ.TYPE_CHECKING:
    from cuprum._subprocess_execution import _SubprocessExecution


def test_wait_for_exit_code_terminates_process_on_timeout() -> None:
    """A timed-out wait terminates the process before propagating TimeoutError.

    ``_wait_for_exit_code`` waits only for the exit code now; on expiry it must
    still tear the process down so the caller's stream consumers can reach EOF,
    while the original ``TimeoutError`` propagates unchanged.
    """

    async def run_case() -> None:
        """Drive ``_wait_for_exit_code`` through its timeout-cleanup branch."""
        process = _TimeoutWaitProcess()
        ctx = ExecutionContext(cancel_grace=0.1)

        # The deadline now belongs to the caller: asyncio.timeout expiry cancels
        # the wait, driving the same cleanup branch and re-raising as
        # TimeoutError at the context-manager boundary.
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await _wait_for_exit_code(
                    typ.cast("asyncio.subprocess.Process", process),
                    ctx,
                    owns_group=False,
                )

        assert process.returncode == -15, (
            "the process must be terminated during timeout cleanup, "
            f"but returncode={process.returncode}"
        )

    asyncio.run(run_case())


def test_wait_for_exit_code_terminates_process_on_cancellation() -> None:
    """Cancelling the waiter terminates the process before CancelledError propagates.

    This covers the distinct ``asyncio.CancelledError`` cleanup path (not the
    timeout path): when the task running ``_wait_for_exit_code`` is cancelled
    mid-wait, cleanup must terminate the process and let ``CancelledError``
    propagate.
    """

    async def run_case() -> None:
        """Cancel ``_wait_for_exit_code`` mid-wait and assert its cleanup."""
        process = _TimeoutWaitProcess()
        ctx = ExecutionContext(cancel_grace=0.1)

        task = asyncio.create_task(
            _wait_for_exit_code(
                typ.cast("asyncio.subprocess.Process", process),
                ctx,
                owns_group=False,
            )
        )
        # Wait until the task has actually entered process.wait() before
        # cancelling, so the cleanup branch is exercised deterministically
        # rather than racing a fixed sleep.
        await asyncio.wait_for(process.wait_started.wait(), timeout=2.0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert process.returncode == -15, (
            "the process must be terminated during cancellation cleanup, "
            f"but returncode={process.returncode}"
        )

    asyncio.run(run_case())


@pytest.mark.parametrize("timeout", [0, 0.0, -1.0])
def test_non_positive_timeout_expires_immediately(timeout: float) -> None:
    """A non-positive deadline times out even when the process already exited.

    ``asyncio.timeout`` schedules its cancellation only for the next event-loop
    iteration, so a process whose ``wait()`` returns without suspending would
    otherwise race past a zero or negative deadline and report success. The
    waiter must instead expire deterministically, preserving the previous
    ``asyncio.wait_for`` behaviour (regression for ``run(timeout=0)``).
    """

    async def run_case() -> None:
        """Drive the waiter with an already-exited process and expect a timeout."""
        process = _ExitedProcess()
        execution = _DeadlineExecution(ctx=ExecutionContext(), timeout=timeout)

        with pytest.raises(TimeoutError):
            await _wait_for_exit_code_within_timeout(
                typ.cast("asyncio.subprocess.Process", process),
                typ.cast("_SubprocessExecution", execution),
            )

    asyncio.run(run_case())


def test_non_positive_timeout_terminates_running_process() -> None:
    """A non-positive deadline terminates a still-running process before raising.

    Stream consumers belong to the caller, which drains them exactly once via
    ``_drain_stream_consumers``; the fast path's own teardown duty is to
    terminate the process so those consumers can reach EOF during that drain.
    """

    async def run_case() -> None:
        """Expire immediately against a running process and assert termination."""
        process = _TimeoutWaitProcess()
        execution = _DeadlineExecution(
            ctx=ExecutionContext(cancel_grace=0.1), timeout=0
        )

        with pytest.raises(TimeoutError):
            await _wait_for_exit_code_within_timeout(
                typ.cast("asyncio.subprocess.Process", process),
                typ.cast("_SubprocessExecution", execution),
            )

        assert process.returncode == -15, (
            "the process must be terminated when a non-positive deadline expires, "
            f"but returncode={process.returncode}"
        )

    asyncio.run(run_case())


class _SigtermImmuneProcess:
    """Process double that ignores ``terminate()``; only ``kill()`` ends it."""

    def __init__(self) -> None:
        """Start unexited, recording which signals were delivered."""
        self.returncode: int | None = None
        self.pid = 7321
        self.terminated = asyncio.Event()
        self.killed = False
        self.wait_started = asyncio.Event()
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        """Block until ``kill()`` records an exit code."""
        self.wait_started.set()
        await self._exited.wait()
        return self.returncode if self.returncode is not None else 0

    def terminate(self) -> None:
        """Signal receipt without exiting, as a SIGTERM-immune child would."""
        self.terminated.set()

    def kill(self) -> None:
        """Record the escalation and exit."""
        self.killed = True
        self.returncode = -9
        self._exited.set()


def test_cancellation_during_grace_still_escalates_to_kill() -> None:
    """A cancel landing in the grace period must not skip the SIGKILL escalation.

    A deadline expiry already consumes one cancellation to reach the teardown,
    so the caller's own ``cancel()`` is delivered to the grace-period wait
    inside it. Unshielded, that skipped the escalation and left a
    ``SIGTERM``-immune child running after the run had returned.
    """

    async def run_case() -> None:
        """Expire the deadline, then cancel while the grace period runs."""
        process = _SigtermImmuneProcess()
        ctx = ExecutionContext(cancel_grace=0.3)

        async def waiter() -> None:
            """Drive the wait under a deadline that expires almost at once."""
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(0.01):
                    await _wait_for_exit_code(
                        typ.cast("asyncio.subprocess.Process", process),
                        ctx,
                        owns_group=False,
                    )

        task = asyncio.create_task(waiter())
        await process.wait_started.wait()
        # Wait for the deadline to fire and teardown to send SIGTERM, so the
        # cancellation below lands inside the grace period rather than before
        # it — no sleep-based guess at the timing.
        await process.terminated.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert process.killed, (
            "the SIGKILL escalation must complete before the cancellation "
            "propagates, otherwise a SIGTERM-immune child outlives the run"
        )
        assert process.returncode == -9, (
            f"the child must be reaped, got returncode={process.returncode!r}"
        )

    asyncio.run(run_case())


def test_the_collection_deadline_is_the_budget_the_exit_wait_left() -> None:
    """The post-exit remainder is the timeout minus the elapsed monotonic time.

    The awaits after the child exits share one deadline, and it is a remainder
    rather than the configured timeout: a run whose exit wait consumed most of
    its budget must not hand the whole of it to what follows. The clock is
    injected so the figure is asserted rather than raced.
    """
    observation = _ClockObservation(reading=2.5)
    execution = _DeadlineExecution(
        ctx=ExecutionContext(),
        timeout=10.0,
        observation=observation,
        started_at=0.5,
    )

    deadline = _deadline_for_collection(typ.cast("_SubprocessExecution", execution))

    assert deadline == pytest.approx(8.0), (
        "the deadline must be the timeout less the 2.0 seconds already spent"
    )
    assert observation.reads == 1, (
        f"the remainder must be measured once, at the point of use; got "
        f"{observation.reads} reads"
    )


def test_an_elapsed_deadline_leaves_a_negative_remainder() -> None:
    """An overrun exit wait is reported as overspent, not silently clamped.

    Clamping to zero would present an exhausted budget as an expiring one and
    lose the fact that the exit wait itself overran, which is the distinction
    an operator reading a post-exit timeout needs.
    """
    execution = _DeadlineExecution(
        ctx=ExecutionContext(),
        timeout=3.0,
        observation=_ClockObservation(reading=5.0),
        started_at=0.0,
    )

    deadline = _deadline_for_collection(typ.cast("_SubprocessExecution", execution))

    assert deadline == pytest.approx(-2.0), (
        f"an overrun must stay negative, got {deadline!r}"
    )


def test_an_unconfigured_timeout_leaves_the_collection_unbounded() -> None:
    """A run with no deadline consults no clock and reports no remainder.

    ``None`` is what keeps the post-exit boundary unbounded here, exactly as an
    absent timeout leaves the exit wait unbounded. Reading the clock first
    would make a run that has no deadline depend on one anyway.
    """
    observation = _ClockObservation(reading=99.0)
    execution = _DeadlineExecution(
        ctx=ExecutionContext(),
        timeout=None,
        observation=observation,
    )

    deadline = _deadline_for_collection(typ.cast("_SubprocessExecution", execution))

    assert deadline is None, (
        f"an unconfigured timeout must stay unbounded, got {deadline!r}"
    )
    assert observation.reads == 0, (
        "a run with no deadline has no remainder to measure, so the clock "
        f"must not be consulted; got {observation.reads} reads"
    )
