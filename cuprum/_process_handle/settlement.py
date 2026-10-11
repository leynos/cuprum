"""The one-settlement rule an owned handle is driven by.

``SafeCmd.start()`` hands its child to a caller that may wait, signal, stop, or
tear it down, and several of those can be in flight at once — a ``wait`` racing
a ``terminate``, a teardown arriving while a concurrent wait is still running.
All of them have to see one outcome, and :class:`_Settlement` is where that is
decided: the single task that drives a run to its end, the record that task
produced, and the rule for how long the handle keeps it.

The rule is what makes the handle's operations composable rather than merely
re-entrant. A caller whose deadline elapses stops the child and records its
outcome *before* raising, so the error reaches the caller whose clock produced
it and nobody else: every later wait, stop, or teardown reads the recorded
outcome, because the child is already gone and an expiry about it would be a
claim about a process that no longer exists.

A settlement that raises rather than recording is a different matter. Nothing
is kept, because there is nothing worth keeping, and the next caller builds a
fresh task rather than being handed a failure another caller already met.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum._command_internals import (
    _finalize_command_run_failure,
    _finalize_command_run_success,
)
from cuprum._process_handle.coordinator import _settle_handle_run

if typ.TYPE_CHECKING:
    from cuprum._process_handle.coordinator import _HandleSettlement
    from cuprum._process_handle.tasks import _HandleRun
    from cuprum._subprocess_spawn import _SubprocessExecution
    from cuprum.sh import CommandResult, TimeoutExpired

__all__ = ["_Settlement"]


class _Settlement:
    """The single settlement of one handle, and the outcome it produced.

    Held by the handle rather than derived from it, because the state that
    matters is exactly the state that must not be duplicated: which task is
    driving the run, what that task produced, and whether it has run at all.

    Three states are reachable, and each transition out of the idle one happens
    at most once. The task is started under the same lock that reads the
    record, so two callers arriving together cannot both decide to start one,
    and a caller arriving after the record exists starts nothing at all.
    """

    __slots__ = ("_handle", "_lock", "_result", "_settled", "_task")

    def __init__(self, handle: _HandleRun) -> None:
        """Hold *handle*'s run, with no settlement yet begun."""
        self._handle = handle
        self._lock = asyncio.Lock()
        #: The outcome a completed settlement left, for ``RunningCommand.result``.
        self._result: CommandResult | None = None
        #: The record a completed settlement left, if it completed.
        self._settled: _HandleSettlement | None = None
        #: The task driving the run, while one is running.
        self._task: asyncio.Task[_HandleSettlement] | None = None

    @property
    def result(self) -> CommandResult | None:
        """The outcome a completed settlement left, if there is one.

        Returns
        -------
        CommandResult | None
            The settled outcome, or ``None`` while the child is still running.
            Never waits; use :meth:`outcome` to block until there is one.
        """
        return self._result

    async def outcome(self, deadline: float | None = None) -> CommandResult:
        """Return the run's outcome, settling it if nobody has yet.

        Parameters
        ----------
        deadline : float | None, default=None
            The deadline this caller is measured against, or ``None`` when it
            supplied none and this settlement is therefore one no clock can
            run out.

        Returns
        -------
        CommandResult
            The outcome of the one settlement, whichever caller started it and
            whichever route — an ordinary exit, a stop, a kill, or another
            caller's deadline — produced it.

        Raises
        ------
        TimeoutExpired
            If *deadline* is what ended the run. The outcome is recorded all
            the same, and readable from :attr:`result`.
        """  # ruff: ignore[docstring-extraneous-exception] - raised here from the settlement this wait drove
        settlement, was_waiting = await self._await_settlement(deadline)
        expiry = _expiry_for(settlement, deadline, was_waiting=was_waiting)
        if expiry is not None:
            raise expiry
        return settlement.result

    async def settle_and_record(self, deadline: float | None) -> _HandleSettlement:
        """Settle the run and keep what it produced; used by the task itself.

        Returns
        -------
        _HandleSettlement
            The command's outcome, also stored for :attr:`result`, together
            with the deadline error when one ended the wait.
        """
        run = self._handle
        # Rebuilt rather than mutated, so a wait's deadline is applied to the
        # copy this run is waited with and never to the bundle the child was
        # spawned from.
        execution = _with_deadline(run.execution, deadline)
        try:
            settlement = await _settle_handle_run(run, execution)
        except BaseException as run_error:
            await _finalize_command_run_failure(
                execution,
                run.tracking,
                None,
                run_error,
            )
            raise
        await _finalize_command_run_success(
            execution,
            run.tracking,
            settlement.result,
        )
        self._result = settlement.result
        return settlement

    async def _await_settlement(
        self,
        deadline: float | None,
    ) -> tuple[_HandleSettlement, bool]:
        """Await the one settlement, driving it if nobody has yet.

        ``was_waiting`` distinguishes the callers a deadline belongs to from
        the ones that merely arrived afterwards. A caller that awaited the live
        settlement was waiting on the child when the clock ran out, so the
        expiry is its to hear about. A caller that found a recorded settlement
        is reading history: the child is already gone, and there is nothing
        left for a fresh deadline to interrupt.

        Returns
        -------
        tuple[_HandleSettlement, bool]
            The settlement's record, and whether this call awaited a
            settlement that was still in progress.
        """
        async with self._lock:
            settlement = self._settled
            if settlement is not None:
                return settlement, False
            settling = self._task
            if settling is None:
                settling = asyncio.ensure_future(self.settle_and_record(deadline))
                self._task = settling
        # Shielded so a cancelled waiter does not take the settlement with it:
        # the task is the handle's, and the next waiter still gets its outcome.
        awaited = await asyncio.shield(settling)
        async with self._lock:
            self._settled = awaited
            self._task = None
        return awaited, True


def _with_deadline(
    execution: _SubprocessExecution,
    deadline: float | None,
) -> _SubprocessExecution:
    """Return *execution* as the bundle a wait is measured against.

    Returns
    -------
    _SubprocessExecution
        The run's own bundle with its timeout replaced. The standing deadline
        was stripped at spawn, so this copy is the only place a wait's deadline
        comes from.
    """
    return dc.replace(execution, timeout=deadline)


def _expiry_for(
    settlement: _HandleSettlement,
    deadline: float | None,
    *,
    was_waiting: bool,
) -> TimeoutExpired | None:
    """Return the expiry a wait owes its caller, if it owes one.

    All three conditions have to hold. The settlement must have ended on a
    deadline, because otherwise there is no expiry to report. The caller must
    have supplied a deadline, because a wait with none cannot be said to have
    run out of time. And the caller must have been waiting on the live
    settlement rather than reading a record, because a caller who arrived
    afterwards is looking at a child that is already gone.

    Returns
    -------
    TimeoutExpired | None
        The expiry to raise, or ``None`` when the caller should read the
        outcome instead.
    """
    if not was_waiting or deadline is None:
        return None
    return settlement.deadline_error
