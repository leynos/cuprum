"""The machinery a stateful settlement machine drives a handle with.

``_running_command_support`` supplies the doubles and
``_running_command_settlement_support`` the judgements drawn from them. This
module supplies the third piece: the loop, the seams, and the per-step folding
that let a machine drive a handle at all. Separating it keeps each file inside
the 400-line cap and, more usefully, leaves the machine's own module about the
orderings it explores.

Import ``_SettlementHarness`` as the leftmost base of a
``RuleBasedStateMachine`` subclass. The hypothesis stateful decorators key
setup and teardown by the bound method's name once it is registered, so the
members below take part in the run exactly as if they were written into the
machine's own class body.

The harness owns the bookkeeping a step folds against: the process it currently
drives, the probe that answers for that process, the loop the steps run on, and
the counters marking where the previous step ended. A machine reads those and
folds each step through ``_close`` or ``_record``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import typing as typ
from unittest import mock

from hypothesis.stateful import initialize, invariant

from cuprum._pipeline_types import _EventDetails
from cuprum._process_handle import settlement as settlement_module
from cuprum.control_observation import observe_control
from cuprum.unittests._running_command_settlement_support import (
    _expected_outcome,
    _reports_a_stop,
    _settled_event,
    _stop_code,
)
from cuprum.unittests._running_command_support import (
    _handle,
    _observe,
    _probe_of,
    _StubProcess,
    _StubRunConfig,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._process_handle.state import RunningCommand
    from cuprum.sh import CommandResult

_STEP_PATIENCE = 5.0  # A step that overruns this has stalled, not been slow.


class _SettlementHarness:
    """Drive one handle with a loop, a probe, and a fold per step."""

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
        # The teardown only cancels these and never reads a value, so they are
        # collected under the value-blind task type: the waiters carry a
        # ``CommandResult`` and the stream consumers carry a payload, and
        # ``Task`` is invariant in its result type.
        tasks: list[asyncio.Task[typ.Any]] = (
            [] if self._waiter is None else [self._waiter]
        )
        # A run owns consumer tasks, and one abandoned mid-drain still holds
        # them: collected here so nothing is reported unawaited at close. The
        # settlement task is read from every handle for the same reason, and not
        # only from the current one: a rule may have replaced ``self._handle``
        # mid-machine, and the runs it replaced still owe the loop their
        # settlement.
        for handle in self._handles:
            tasks.extend(handle._run.ownership.consumers)
            settling = handle._settlement._task
            if settling is not None:
                tasks.append(settling)
        for task in tasks:
            task.cancel()
        self._drive(asyncio.gather(*tasks, return_exceptions=True))
        self._probe.stop()
        self._registration.detach()
        self._loop.close()
        # Leave the thread as the rules found it: no loop installed, so a later
        # test's ``asyncio.run`` never inherits this machine's closed loop.
        asyncio.set_event_loop(None)
