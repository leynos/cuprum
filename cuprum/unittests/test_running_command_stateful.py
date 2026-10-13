"""Stateful property checks for the one outcome an owned handle records.

A ``wait`` with or without a deadline, a ``terminate``, a ``kill``, and the
teardown of an ``async with`` block share one settlement; this machine draws the
orderings the fixed cases only pin. One outcome, the identical ``CommandResult``
for every caller that did not raise, and no stopped child left unsettled.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import signal
import typing as typ
from unittest import mock

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from cuprum._pipeline_types import _EventDetails
from cuprum._process_handle import settlement as settlement_module
from cuprum.control_observation import observe_control
from cuprum.sh import SignalAfterExitError, TimeoutExpired
from cuprum.unittests._running_command_settlement_support import (
    _expected_outcome,
    _reports_a_stop,
    _settled_event,
    _stop_code,
)
from cuprum.unittests._running_command_support import (
    _EARLY_EXIT,
    _ESCALATION_GRACE,
    _WAITERS,
    _handle,
    _observe,
    _probe_of,
    _SigtermImmuneProcess,
    _StubProcess,
    _StubRunConfig,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._process_handle.state import RunningCommand
    from cuprum.sh import CommandResult

_DEADLINE = 0.02  # Long enough to be a real wait, short enough to expire.
_STEP_PATIENCE = 5.0  # A step that overruns this has stalled, not been slow.
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


class _RunningCommandSettlementMachine(RuleBasedStateMachine):
    """Drive random operations through one handle's settlement and its model."""

    def __init__(self) -> None:
        """Install a fresh loop, the double's probe, and the model's state."""
        super().__init__()
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        process = _StubProcess()
        self._process = process
        # The probe asks the kernel about a PID, which a double does not have.
        self._probe = mock.patch.object(
            settlement_module, "_observed_exit_code", _probe_of(process)
        )
        self._probe.start()
        self._controls: list[object] = []
        self._registration = observe_control(self._controls.append)
        self._events, self._observer = _observe()
        self._handles: list[RunningCommand] = []
        self._handle = self._assembled(process)
        self._observed: list[CommandResult] = []
        self._recorded: CommandResult | None = None
        self._refused = False
        self._in_block = False
        # The rules split the block's two halves across steps, which is what an
        # ``async with`` cannot do. The stack holds the half-open entry between
        # them, so the exit still runs from the block's own boundary rather
        # than from a direct call the rules alone would be able to make.
        self._stack = contextlib.AsyncExitStack()
        self._waiter: asyncio.Task[CommandResult] | None = None
        self._step_graceful = 0
        self._step_forced = 0
        self._close()

    def _assembled(
        self,
        process: _StubProcess,
        config: _StubRunConfig | None = None,
    ) -> RunningCommand:
        """Build a handle over *process*, collecting its events and plan."""
        handle = _handle(process) if config is None else _handle(process, config)
        self._handles.append(handle)
        observation = handle._run.execution.observation
        observation.hooks = dc.replace(
            observation.hooks, observe_hooks=(self._observer,)
        )
        # The plan goes out first: a run publishes its terminal event only after.
        observation.emit("plan", _EventDetails(pid=process.pid))
        return handle

    def _drive[T](self, awaitable: cabc.Awaitable[T]) -> T:
        """Run one awaitable to completion on this machine's own loop."""
        bounded = asyncio.wait_for(awaitable, _STEP_PATIENCE)
        return self._loop.run_until_complete(
            typ.cast("cabc.Coroutine[object, object, T]", bounded)
        )

    def _in_flight(self) -> bool:
        """Whether a settlement is running and has not concluded."""
        task = self._handle._settlement._task
        return task is not None and not task.done()

    def _close(self, *, timed_out: bool = False) -> None:
        """Fold this step into the model, checking every claim it makes.

        Also the one place the step boundary moves: the counters a stop of this
        step moved are the counters already accounted for by the next one.
        """
        graceful, forced = self._step_graceful, self._step_forced
        self._step_graceful = self._process.terminate_calls
        self._step_forced = self._process.kill_calls
        moved = self._step_graceful > graceful or self._step_forced > forced
        if timed_out:
            report = "timeout"
        elif self._in_flight() or self._recorded is not None:
            self._check_repeat(moved=moved)
            return
        elif moved:
            report = "killed" if self._step_forced > forced else "terminated"
        elif self._process.returncode is not None:
            report = "ordinary"
        else:
            assert self._handle.result is None, "an untouched run has no outcome"
            return
        result = self._handle.result
        assert result is not None, f"a {report} step must record an outcome"
        self._recorded = result
        self._assert_ending(report, result, graceful)

    def _check_repeat(self, *, moved: bool) -> None:
        """Check a step folded over an outcome the model already holds.

        A run settles once, so a step arriving after that point must not have
        moved the child again — and whatever the handle records now is the
        very outcome the model recorded when the run first settled.
        """
        if self._recorded is not None:
            assert self._handle.result is self._recorded, "a run keeps its outcome"
            assert not moved, "a settled handle is not signalled again"

    def _assert_ending(
        self,
        report: str,
        result: CommandResult,
        graceful: int,
    ) -> None:
        """Check the recorded code, the run's terminal event, and its kind."""
        code = result.exit_code
        if report == "timeout":
            assert code == self._process.returncode, "a deadline keeps the child's end"
        elif report == "ordinary":
            assert not _reports_a_stop(result), "an own ending reports no signal"
        else:
            stop = _stop_code(
                terminated=self._process.terminate_calls,
                grace=graceful,
            )
            assert code == stop, f"a {report} step records its stop's own status"
        event = _settled_event(self._events)
        assert event is not None, "a recorded outcome publishes a terminal event"
        expected = _expected_outcome(report, code)
        assert event.terminal_outcome is expected, (
            f"a {report} ending must publish {expected}, got {event.terminal_outcome}"
        )
        assert event.exit_code == code, "the event reports that same code"

    def _record(self, task: asyncio.Task[CommandResult]) -> None:
        """Keep what one caller observed, when it observed anything at all.

        Every call site drives the task to completion before recording it, so
        the task is settled and both queries below are answerable. A caller
        that was cancelled, or that failed, observed no outcome and is passed
        over.
        """
        if not task.cancelled() and task.exception() is None:
            self._observed.append(task.result())

    @initialize()
    def _start(self) -> None:
        """Begin with a child that has not been touched."""
        self._close()

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

    @invariant()
    def one_settlement_serves_every_caller(self) -> None:
        """Check the handle, its callers, and the model share one outcome."""
        settlement = self._handle._settlement
        assert self._handle.result is settlement._result, (
            "the handle's result must be the settlement's own record"
        )
        if self._recorded is not None:
            assert settlement._result is self._recorded, (
                "the recorded outcome must be the settlement's own"
            )
        for observed in self._observed:
            assert observed is self._recorded, (
                "every caller that did not raise sees the identical CommandResult"
            )

    @invariant()
    def a_run_publishes_one_terminal_event(self) -> None:
        """Check a run publishes its outcome once, and never a second time."""
        settled = [item for item in self._events if item.phase == "settled"]
        assert len(settled) <= 1, (
            f"a run publishes one terminal event, got {len(settled)}"
        )

    def teardown(self) -> None:
        """Cancel anything still running, restore the seams, and close the loop."""
        if self._in_block:
            # A run that ended inside the block still owes it an exit; leaving
            # the entry on the stack would drop the stop and settle the child
            # by cancellation alone, which is the one path the handle exists to
            # prevent.
            self._drive(self._stack.aclose())
        settling = self._handle._settlement._task
        # The teardown only cancels these and never reads a value, so they are
        # collected under the value-blind task type: the waiters carry a
        # ``CommandResult`` and the stream consumers carry a payload, and
        # ``Task`` is invariant in its result type.
        tasks: list[asyncio.Task[typ.Any]] = [
            task for task in (self._waiter, settling) if task is not None
        ]
        # A run owns consumer tasks, and one abandoned mid-drain still holds
        # them: collected here so nothing is reported unawaited at close.
        for handle in self._handles:
            tasks.extend(handle._run.ownership.consumers)
        for task in tasks:
            task.cancel()
        self._drive(asyncio.gather(*tasks, return_exceptions=True))
        self._probe.stop()
        self._registration.detach()
        self._loop.close()
        # Leave the thread as the rules found it: no loop installed, so a later
        # test's ``asyncio.run`` never inherits this machine's closed loop.
        asyncio.set_event_loop(None)


TestRunningCommandSettlement = _RunningCommandSettlementMachine.TestCase
TestRunningCommandSettlement.settings = settings(
    max_examples=40,
    stateful_step_count=16,
    deadline=None,
)
