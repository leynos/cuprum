"""Stateful property checks for the one outcome an owned handle records.

A ``wait`` with or without a deadline, a ``terminate``, a ``kill``, and the
teardown of an ``async with`` block share one settlement; this machine draws the
orderings the fixed cases only pin. One outcome, the identical ``CommandResult``
for every caller that did not raise, and no stopped child left unsettled.

What stays here are the orderings. The loop, seams, and per-step folding live
in ``_running_command_settlement_harness_support``, and the judgements drawn from a
stop's effect live in ``_running_command_settlement_support``.
"""

from __future__ import annotations

import asyncio
import signal

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
)

from cuprum.sh import SignalAfterExitError, TimeoutExpired
from cuprum.unittests._running_command_settlement_harness_support import (
    _SettlementHarness,
)
from cuprum.unittests._running_command_support import (
    _EARLY_EXIT,
    _ESCALATION_GRACE,
    _WAITERS,
    _SigtermImmuneProcess,
    _StubRunConfig,
)

_DEADLINE = 0.02  # Long enough to be a real wait, short enough to expire.
# Caller-named signals to deliver, none of them a stop: those settle the run.
# Sourced by name so this module imports on any platform; a name the host does
# not define is simply not drawn, rather than raising while pytest collects.
_SIGNAL_NAMES = ("SIGHUP", "SIGUSR1", "SIGCONT")
if not all(hasattr(signal, name) for name in _SIGNAL_NAMES):
    pytest.skip(
        "these POSIX signals are not defined on this platform",
        allow_module_level=True,
    )
_SIGNAL_NUMBERS = tuple(getattr(signal, name) for name in _SIGNAL_NAMES)


class _RunningCommandSettlementMachine(_SettlementHarness, RuleBasedStateMachine):
    """Draw the orderings one handle's settlement has to survive."""

    @precondition(lambda self: self._waiter is None)
    @rule(timeout=st.sampled_from((None, 0.0, _DEADLINE)))
    def wait_for_the_child(self, timeout: float | None) -> None:
        """Wait for the child, naming no deadline or the one this step draws."""
        waiter = asyncio.ensure_future(self._handle.wait(timeout=timeout))
        self._drive(asyncio.sleep(0))
        if timeout is None and not waiter.done():
            self._waiter = waiter  # A later rule ends the child this one parked.
            self._close()
            return
        expired = None
        try:
            self._drive(waiter)
        except TimeoutExpired as deadline_expired:
            expired = deadline_expired
        if expired is not None:
            assert expired.timeout == timeout, "the expiry carries this very deadline"
            self._close(timed_out=True)
            return
        self._record(waiter)
        self._close()

    @precondition(lambda self: self._waiter is None and self._recorded is None)
    @rule()
    def cancel_a_waiting_caller(self) -> None:
        """Cancel a caller parked on the settlement every caller shares."""
        waiter = asyncio.ensure_future(self._handle.wait())
        self._drive(asyncio.sleep(0))
        assert not waiter.done(), "the waiter must be parked on the settlement"
        waiter.cancel()
        for _ in range(2):
            self._drive(asyncio.sleep(0))
        if not waiter.done():
            self._drive(asyncio.gather(waiter, return_exceptions=True))
        assert waiter.cancelled(), "a cancelled caller must end as cancelled"
        assert self._handle.result is None, "a cancelled caller records nothing"
        self._close()

    @rule(signal_number=st.sampled_from(_SIGNAL_NUMBERS))
    def signal_the_child(self, signal_number: int) -> None:
        """Deliver a caller-named signal, which must not settle the run."""
        try:
            self._drive(self._handle.signal(signal_number))
        except SignalAfterExitError:
            assert self._process.returncode is not None, (
                "a refusal is about a child that has already exited"
            )
        else:
            assert self._process.signals[-1] == signal_number, "the signal reached it"
            assert self._handle.result is self._recorded, "a signal settles nothing"
        self._close()

    @precondition(lambda self: not self._in_block)
    @rule(
        mode=st.sampled_from(("graceful", "forced")),
        grace=st.sampled_from((0.0, _ESCALATION_GRACE)),
    )
    def stop_the_child(self, mode: str, grace: float) -> None:
        """Stop the child gracefully, escalating, or compel it to end at once."""
        if mode == "graceful":
            self._drive(self._handle.terminate(grace=grace))
        else:
            self._drive(self._handle.kill())
        self._close()

    @precondition(
        lambda self: (
            not self._in_block
            and self._process.returncode is None
            and self._waiter is None
        )
    )
    @rule()
    def cancel_a_stop_inside_its_grace(self) -> None:
        """Cancel a caller whose graceful stop is waiting out its grace window."""
        immune = _SigtermImmuneProcess()
        self._handle = self._assembled(immune, _StubRunConfig(grace=_ESCALATION_GRACE))
        self._process = immune
        stopping = asyncio.ensure_future(self._handle.terminate())
        self._drive(asyncio.sleep(0))
        assert immune.signalled.is_set(), "the stop must be inside its grace window"
        assert not stopping.done(), "the stop must still be waiting the child out"
        stopping.cancel()
        try:
            self._drive(stopping)
        except asyncio.CancelledError:
            pass
        else:
            msg = "the cancelled caller must see its own cancellation"
            raise AssertionError(msg)
        assert stopping.cancelled(), "the cancelled caller ends as cancelled"
        assert immune.terminate_calls == 1, "the stop asked the child once"
        assert immune.kill_calls == 1, "the escalation still ran before the cancel"
        # This child ignores SIGTERM, so its status is the forced signal's.
        self._step_graceful = immune.terminate_calls
        self._close()

    @precondition(lambda self: not self._in_block)
    @rule()
    def enter_the_block(self) -> None:
        """Enter the handle's ``async with`` block without leaving it."""
        entered = self._drive(self._stack.enter_async_context(self._handle))
        assert entered is self._handle, "the block yields the handle it opened"
        self._in_block = True
        self._close()

    @precondition(lambda self: self._in_block)
    @rule()
    def leave_the_block(self) -> None:
        """Leave the block, at the boundary every ``async with`` exits through."""
        before = len(self._controls)
        # The stack ran the enter, so closing it runs the matching exit — the
        # same ``__aexit__`` an ``async with`` block reaches, at the same point
        # in the teardown. Calling the dunder outright would be the rules
        # reaching past the boundary they are meant to be exercising.
        self._drive(self._stack.aclose())
        self._in_block = False
        assert len(self._controls) == before, "teardown reports no control operation"
        assert self._process.terminate_calls - self._step_graceful <= 1, (
            "teardown stops the child rather than signalling it twice"
        )
        self._close()

    @precondition(
        lambda self: self._recorded is None and self._process.returncode is None
    )
    @rule(code=st.integers(min_value=0, max_value=9))
    def let_the_child_exit(self, code: int) -> None:
        """End the child, then collect the caller already parked on it."""
        waiter, self._waiter = self._waiter, None
        self._process.exit_with(code)
        if waiter is None:
            result = self._drive(self._handle.wait())
            assert result.exit_code == code, "an unstopped child reports its own code"
            self._close()
            return
        self._drive(asyncio.gather(waiter, return_exceptions=True))
        self._record(waiter)
        if self._handle.result is None:
            self._refused = True  # An after hook refused the run's outcome.
        self._close()

    @precondition(
        lambda self: (
            self._recorded is None
            and self._waiter is None
            and self._process.returncode is None
        ),
    )
    @rule()
    def gather_the_waiters(self) -> None:
        """Hand several callers to the one settlement at the same time."""
        waiters = [asyncio.ensure_future(self._handle.wait()) for _ in range(_WAITERS)]
        self._drive(asyncio.sleep(0))
        assert all(not waiter.done() for waiter in waiters), "every waiter is pending"
        self._process.exit_with(_EARLY_EXIT)
        self._drive(asyncio.gather(*waiters))
        for waiter in waiters:
            self._record(waiter)
        self._close()

    @invariant()
    def a_stop_is_delivered_once_and_settles_the_run(self) -> None:
        """Check a stop happens once, on a live child, and settles the handle."""
        process = self._process
        assert process.terminate_calls <= 1, "SIGTERM is delivered at most once"
        assert process.kill_calls <= 1, "SIGKILL is delivered at most once"
        if process.returncode is None:
            return
        assert self._in_flight() or self._handle.result is not None or self._refused, (
            "a child that has ended must be settling or settled"
        )


TestRunningCommandSettlement = _RunningCommandSettlementMachine.TestCase
TestRunningCommandSettlement.settings = settings(
    max_examples=40,
    stateful_step_count=16,
    deadline=None,
)
