"""The one-settlement rule an owned handle is driven by.

``SafeCmd.start()`` hands its child to a caller that may wait, signal, stop, or
tear it down, and several of those can be in flight at once — a ``wait`` racing
a ``terminate``, a teardown arriving while a concurrent wait is still running.
All of them have to see one outcome, and :class:`_Settlement` is where that is
decided: the single task that drives a run to its end, the outcome that task
produced, and the rule for who hears about a deadline.

The task carries no deadline of its own. It waits for the child, and the child
only ends early because some caller stopped it, so the task is a plain wait and
each caller bounds *its own* await of that shared task. That is what makes
``wait(timeout=...)`` a limit on one caller rather than on the handle: a caller
that names a deadline and runs out of time stops the child, records the outcome
that stop produced, and raises its own expiry, while every other caller — the
one that named no deadline, the one that named a longer one, the one that
arrived after — simply reads the one recorded outcome.

Because the deadline belongs to the caller, the error does too. The expiry is
built from the caller's own deadline and the outcome the shared settlement
recorded, drained output included, so a caller that expired learns how the
child ended as well as that its clock ran out.

The deadline is recorded on the settlement before the child is stopped, so the
one settlement finalizes as the timeout it was: the terminal outcome a consumer
observes for a deadline-ended run is ``TIMEOUT``, matching what the streamed
``run()`` path reports for the same condition.

A deadline that fires after the child has exited is not that case, so it is not
recorded. The settlement can outlive the child — the drain waits out an EOF
grace, which a grandchild holding an inherited pipe can extend — and a deadline
landing there would report a timeout, and skip the run's ``after`` hooks, for a
child that ended on its own. The caller is given the child's own ending
instead, exactly as the streamed path's exit-wait deadline does.

A settlement that raises rather than recording is a different matter. Nothing
is kept, because there is nothing worth keeping, and the failed task stays where
it is: a later caller awaits the same task and meets the same failure, which is
the error the first caller never got to return.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._command_internals import (
    _finalize_command_run_failure,
    _finalize_command_run_success,
)
from cuprum._process_handle.control import _terminate_shielded
from cuprum._process_handle.coordinator import _settle_handle_run
from cuprum._subprocess_context import _sh_module

if typ.TYPE_CHECKING:
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
    outcome, so two callers arriving together cannot both decide to start one,
    and a caller arriving after the outcome exists starts nothing at all.
    """

    __slots__ = ("_deadline", "_handle", "_lock", "_result", "_task")

    def __init__(self, handle: _HandleRun) -> None:
        """Hold *handle*'s run, with no settlement yet begun."""
        self._handle = handle
        self._lock = asyncio.Lock()
        #: The deadline the first expiring caller named, if one has expired.
        self._deadline: float | None = None
        #: The outcome a completed settlement left, for ``RunningCommand.result``.
        self._result: CommandResult | None = None
        #: The task driving the run, while one is running or has failed.
        self._task: asyncio.Task[CommandResult] | None = None

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
            supplied none and its wait is therefore one no clock can run out.

        Returns
        -------
        CommandResult
            The outcome of the one settlement, whichever caller started it and
            whichever route — an ordinary exit, a stop, a kill, or another
            caller's deadline — produced it.

        Raises
        ------
        TimeoutExpired
            If *deadline* elapsed before the child finished. The child is
            stopped and reaped, the outcome is recorded, and a later caller
            reads that outcome rather than meeting this error again.
        """  # ruff: ignore[docstring-extraneous-exception] - raised here from the deadline this wait drove
        if deadline is None:
            return await self._await_settlement()
        if deadline <= 0:
            # An already-elapsed deadline expires deterministically: the child
            # is never given the chance to exit on its own first, matching the
            # non-positive branch of the streamed wait.
            return await self._expire(deadline)
        try:
            async with asyncio.timeout(deadline):
                return await self._await_settlement()
        except TimeoutError:
            # Only this caller's clock ran out. The settlement task is shielded
            # from the cancellation the timeout delivered, so it is still
            # driving the run and the child is stopped here rather than left.
            return await self._expire(deadline)

    async def settle_and_record(self) -> CommandResult:
        """Settle the run and keep what it produced; used by the task itself.

        Returns
        -------
        CommandResult
            The command's outcome, also stored for :attr:`result`.

        Raises
        ------
        BaseException
            Whatever ended the run early: the failure the wait itself hit, or a
            caller's `after` hook refusing the outcome, once the run's framing
            has been closed behind it.
        """  # ruff: ignore[docstring-extraneous-exception] - the guard re-raises whatever the run or a hook raised
        run = self._handle
        # The bundle the child was spawned from, with no deadline in it: the
        # task is driven by no clock, so nothing here can expire on its own.
        execution = run.execution
        try:
            result = await _settle_handle_run(run)
        except BaseException as run_error:
            await _finalize_command_run_failure(
                execution,
                run.tracking,
                None,
                run_error,
            )
            raise
        deadline = self._deadline
        if deadline is None:
            # The after hooks pair with the before hooks `_prepare_owned_command`
            # already ran, and they run before the run is finalized so a failing
            # hook settles the run as the error it was rather than being
            # swallowed by a success. A deadline-ended run takes the other
            # branch: the streamed path does not run its hooks on a timeout
            # either, because the caller is owed an error rather than a record.
            try:
                for hook in run.tracking.execution_hooks.after_hooks:
                    hook(execution.cmd, result)
            except BaseException as hook_error:
                await _finalize_command_run_failure(
                    execution,
                    run.tracking,
                    result,
                    hook_error,
                )
                raise
            await _finalize_command_run_success(execution, run.tracking, result)
        else:
            # A caller's deadline is what stopped the child, so the run settles
            # as the timeout it was rather than as the ordinary non-zero exit
            # the signal itself would report.
            await _finalize_command_run_failure(
                execution,
                run.tracking,
                result,
                _expiry_error(execution, result, deadline),
            )
        self._result = result
        return result

    async def _await_settlement(self) -> CommandResult:
        """Await the one settlement, driving it if nobody has yet.

        Returns
        -------
        CommandResult
            The outcome the shared settlement recorded.
        """
        async with self._lock:
            settled = self._result
            if settled is not None:
                return settled
            settling = self._task
            if settling is None:
                settling = asyncio.ensure_future(self.settle_and_record())
                self._task = settling
        # Shielded so a cancelled waiter does not take the settlement with it:
        # the task is the handle's, and the next waiter still gets its outcome.
        awaited = await asyncio.shield(settling)
        async with self._lock:
            self._result = awaited
            self._task = None
        return awaited

    async def _expire(self, deadline: float) -> CommandResult:
        """Stop the child this caller gave up on and raise this caller's expiry.

        The deadline is recorded before the child is stopped, so the settlement
        task — which is what finalizes the run — can tell that a clock rather
        than the child ended it. Stopping an already-stopped child delivers
        nothing: the stop path short-circuits on a child that has exited, so a
        second expiry neither signals twice nor builds a second outcome.

        A positive deadline that fires after the child has already exited is
        not recorded and stops nothing. The settlement is still finishing, and
        what it is finishing is the drain — up to the capture EOF grace, or
        longer while a grandchild holds an inherited pipe open. Recording the
        deadline there would publish ``TIMEOUT`` for a child that ended on its
        own and skip its ``after`` hooks, so the child's own ending is what the
        caller is given instead. The streamed ``run()`` path applies its
        deadline the same way, to the exit wait alone. A non-positive deadline
        is exempt from the exemption and still expires deterministically, as
        the documented contract for an already-elapsed deadline requires.

        Returns
        -------
        CommandResult
            The recorded outcome when the run had already settled, or the
            outcome the child produced on its own when the deadline fired
            after it had exited. History, either way, rather than something a
            fresh deadline can interrupt.

        Raises
        ------
        TimeoutExpired
            The expiry the caller's own deadline produced, carrying the output
            the settled run captured.
        """  # ruff: ignore[docstring-missing-exception, docstring-extraneous-exception] - the raise names a factory the call returns a TimeoutExpired from
        run = self._handle
        async with self._lock:
            settled = self._result
            if settled is not None:
                return settled
            exited_first = deadline > 0 and run.process.returncode is not None
            if not exited_first:
                self._record_deadline(deadline)
            settling = self._task
            if settling is None:
                settling = asyncio.ensure_future(self.settle_and_record())
                self._task = settling
        if exited_first:
            # Unbounded and shielded, for the reason the stop path below is:
            # the task is the only thing that reaps the child and drains its
            # streams, so walking away here would strand both.
            return await self._await_settlement()
        await _terminate_shielded(run.process, run.execution.ctx.cancel_grace)
        # Unbounded: the child is already stopping, and the task is the only
        # thing that reaps it, so waiting here is what guarantees the child
        # this caller gave up on does not outlive the error it is owed.
        result = await asyncio.shield(settling)
        async with self._lock:
            self._result = result
            self._task = None
        raise _expiry_error(run.execution, result, deadline)

    def _record_deadline(self, deadline: float) -> None:
        """Keep the first deadline that expired, for the settlement to finalize with."""
        if self._deadline is None:
            self._deadline = deadline


def _expiry_error(
    execution: _SubprocessExecution,
    result: CommandResult,
    deadline: float,
) -> TimeoutExpired:
    """Build the expiry a deadline-ran-out caller is owed.

    The command and the deadline are the caller's own; the output is the
    outcome the shared settlement drained, so the expiry reports what the child
    had written before it was stopped — exactly the contract the streamed
    ``run()`` path's ``TimeoutExpired`` keeps.

    Returns
    -------
    TimeoutExpired
        The report of the expiry, carrying the drained streams.
    """
    return _sh_module().TimeoutExpired(
        cmd=execution.cmd.argv_with_program,
        timeout=deadline,
        output=result.stdout,
        stderr=result.stderr,
    )
