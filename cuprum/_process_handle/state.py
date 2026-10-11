"""The owned handle to a running command.

``RunningCommand`` is what ``SafeCmd.start()`` hands back: a child already
spawned, already announced, and owned outright. The handle exists because the
alternative — deriving a PID from a ``start`` observer and signalling it by
hand — leaves the caller responsible for races the library can see and they
cannot: a child that exits before the first signal, a PID that has been reused,
a set of stream consumers nobody is draining.

Ownership is the whole point, so the handle holds every piece that ownership
implies: the process, the bundle its streams and deadline came from, the
tracking that owns its sink bracket and observe-hook tasks, and the task
ownership that reconciles its stdin writer and stream consumers.

Settlement happens once. ``self._settling`` holds the single task that drives
a run to its end, and every operation that needs an outcome awaits that same
task rather than starting a second one. That is what makes concurrent ``wait``
calls a single settlement with several readers, and it is also what keeps
``terminate`` from racing a wait: stopping the child first unblocks the shared
task, which then completes normally and hands every waiter the result.

Because the task is a real task rather than a lock held across the wait, a
caller who is cancelled leaves the settlement running; the next caller awaits
the same task and gets the same outcome, whether that is a result or the error
the first caller never saw. Nothing here can deadlock a stop behind a wait that
has no deadline.

A wait that expires is the one route that produces both an outcome and an
error, and the handle keeps both rather than letting the task's exception
decide for every later caller. The child is stopped and reaped by the time the
deadline is reported, so its outcome is recorded first and the error raised
afterwards — which is why the settlement task *completes* on a deadline, with
the error carried in the settlement rather than thrown out of it. A later
``wait``, ``terminate``, or ``kill`` on the same handle therefore sees the
outcome the first caller's deadline denied them, instead of a resurrected
exception describing a child that is already gone.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum._command_internals import (
    _finalize_command_run_failure,
    _finalize_command_run_success,
)
from cuprum._process_handle.control import (
    _deliver_signal,
    _kill_shielded,
    _terminate_shielded,
)
from cuprum._process_handle.coordinator import _settle_handle_run
from cuprum._process_lifecycle import _shielded_cleanup

if typ.TYPE_CHECKING:
    import types

    from cuprum._process_handle.coordinator import _HandleSettlement
    from cuprum._process_handle.tasks import _HandleRun
    from cuprum._subprocess_spawn import _SubprocessExecution
    from cuprum.events import ExecId
    from cuprum.sh import CommandResult, TimeoutExpired

__all__ = ["RunningCommand"]


class RunningCommand:
    """An owned handle to a command that is already running.

    Returned by :meth:`SafeCmd.start`. Unlike :meth:`SafeCmd.run`, which
    awaits a child to completion, this handle keeps the child alive and hands
    the caller explicit control over it: :meth:`wait` for the outcome,
    :meth:`signal` to deliver a POSIX signal, :meth:`terminate` for the
    graceful stop, and :meth:`kill` for the immediate one.

    The handle owns the child's lifetime. Leaving an ``async with`` block
    settles it deterministically — a child still running is stopped down the
    same ``SIGTERM``, cancel-grace, ``SIGKILL`` path a cancelled ``run()``
    takes — so no child is left running or unreaped because a caller forgot to
    stop it.

    Every operation that settles is idempotent and safe to repeat, and none of
    them suppresses the exception of an enclosing block. Once the child has
    exited, :attr:`result` holds the same ``CommandResult`` a ``run()`` call
    would have returned, and further waits or terminations report that outcome
    rather than failing.

    A deadline is the one exception, and it is raised once. The wait whose
    clock ran out reports :class:`~cuprum.sh.TimeoutExpired` after stopping and
    reaping the child; because that work is done, the outcome it produced is
    recorded, and a later wait, termination, or teardown returns it instead of
    re-raising an error about a child that is already gone.
    """

    __slots__ = ("_lock", "_result", "_run", "_settled_run", "_settling")

    def __init__(self, run: _HandleRun) -> None:
        """Take ownership of a spawned child and its execution record."""
        self._run = run
        self._lock = asyncio.Lock()
        self._result: CommandResult | None = None
        self._settled_run: _HandleSettlement | None = None
        self._settling: asyncio.Task[_HandleSettlement] | None = None

    @property
    def pid(self) -> int | None:
        """The child's process identifier, as announced by its ``start`` event.

        Returns
        -------
        int | None
            The child's PID, or ``None`` on a platform whose transport never
            reported one.
        """
        return self._run.process.pid

    @property
    def exec_id(self) -> ExecId:
        """The execution identifier every event from this run carries.

        Returns
        -------
        ExecId
            The identifier shared by the run's ``plan``, ``start``, ``exit``,
            and ``settled`` events, so a consumer can correlate the handle
            with the observation stream that describes it.
        """
        return self._run.execution.observation.exec_id

    @property
    def result(self) -> CommandResult | None:
        """The settled outcome, or ``None`` while the command is still running.

        Returns
        -------
        CommandResult | None
            The command's result once settlement has completed, otherwise
            ``None``. The property never waits; use :meth:`wait` to block until
            there is an outcome to read. A wait whose deadline elapsed
            publishes the outcome here too, because the child it stopped is
            nonetheless reaped and has one.
        """
        return self._result

    async def wait(
        self,
        *,
        timeout: float | None = None,  # ruff: ignore[async-function-with-timeout]  # The deadline bounds this call, and asyncio.timeout cannot serve an awaitable a handle owns.
    ) -> CommandResult:
        """Wait for the command to finish and return its outcome.

        The first caller to arrive settles the run; every later caller awaits
        the same settlement and sees the same outcome, so calling this
        repeatedly or concurrently is safe and reports one result.

        Parameters
        ----------
        timeout : float | None, default=None
            Seconds to allow the command to finish. Expiry stops the child the
            way a timed-out ``run()`` does and raises
            :class:`~cuprum.sh.TimeoutExpired` carrying the output captured
            before the deadline. ``None`` falls back to the deadline
            :meth:`SafeCmd.start` resolved for this handle, and to no deadline
            at all when ``start()`` resolved none.

        Returns
        -------
        CommandResult
            The command's outcome once it has exited.

        Raises
        ------
        TimeoutExpired
            If *timeout*, or the deadline ``start()`` resolved, elapsed before
            the command finished.
        """  # ruff: ignore[docstring-extraneous-exception] - TimeoutExpired propagates from the deadline path
        return await self._settled(self._default_deadline(timeout))

    def _default_deadline(self, timeout: float | None) -> float | None:
        """Return *timeout*, or the deadline ``start()`` resolved in its place.

        A handle is waited on by a caller who may never see the value
        ``start()`` resolved, so the standing deadline travels on the run and
        is consulted here rather than being lost with the bundle it was
        stripped from.

        Returns
        -------
        float | None
            The deadline this wait is measured against, or ``None`` when
            neither the caller nor ``start()`` supplied one.
        """
        return self._run.default_timeout if timeout is None else timeout

    async def signal(self, signal_number: int) -> None:
        """Deliver a POSIX signal to the running command.

        Unlike :meth:`terminate` and :meth:`kill`, this asks for exactly the
        signal named and takes no view on how the child should respond. It does
        not settle the run: a child sent ``SIGSTOP``, or sent a signal it
        ignores, is still the caller's to wait on or stop.

        Parameters
        ----------
        signal_number : int
            The signal to deliver, as :mod:`signal` spells it.

        Raises
        ------
        SignalAfterExitError
            If the command has already exited, carrying its exit code.
        UnsupportedSignalError
            If the platform cannot deliver a caller-named signal.
        """  # ruff: ignore[docstring-extraneous-exception] - both refusals are raised by the delivery helper
        _deliver_signal(self._run, signal_number)

    async def terminate(self, *, grace: float | None = None) -> CommandResult:
        """Stop the command gracefully, escalating if it does not comply.

        The child is sent ``SIGTERM``, given *grace* seconds, then sent
        ``SIGKILL`` if it is still there. Both the signal that ended it and the
        output it produced before ending are preserved: a child that exited on
        the ``SIGTERM`` reports ``-15``, and one that had to be killed reports
        ``-9``.

        Calling this on a command that has already exited is a no-op that
        reports the recorded outcome — including when a ``wait`` deadline is
        what ended the child, since the outcome that wait produced is the one
        this returns.

        Parameters
        ----------
        grace : float | None, default=None
            Seconds to wait between the graceful signal and the forced one.
            Defaults to the execution context's cancel grace, which is the
            same window a cancelled ``run()`` allows.

        Returns
        -------
        CommandResult
            The ended command's outcome.
        """
        await self._stop(grace)
        return await self._settled(None)

    async def kill(self) -> CommandResult:
        """Stop the command immediately, without a grace period.

        This is the forced half of :meth:`terminate` on its own: the child is
        sent ``SIGKILL`` and reaped, so its outcome reports ``-9`` unless it
        had already exited, in which case the recorded outcome is returned.
        Unlike :meth:`terminate`, no ``SIGTERM`` is sent first, so the reported
        signal is the one the caller asked for rather than whichever one
        happened to end the child.

        Returns
        -------
        CommandResult
            The ended command's outcome.
        """
        await _kill_shielded(self._run.process)
        return await self._settled(None)

    async def __aenter__(self) -> RunningCommand:
        """Return self, so ``async with`` owns the child for the block."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: types.TracebackType | None,
    ) -> None:
        """Settle the child on every exit from the ``async with`` block.

        Never suppresses: the block's own exception reaches the caller
        unchanged. A teardown failure is reported only when the block itself
        succeeded — otherwise it would replace the error that caused the exit —
        and it is recorded on the run's event stream either way.

        Settlement is attempted even when the stop did not complete cleanly,
        and that ordering is deliberate. The two steps are separate *try*
        blocks rather than one, because a cancellation arriving during the
        stop would otherwise skip the settle entirely: the child would be
        reaped but no result recorded, leaving a handle whose :attr:`result`
        stays ``None`` forever. Reaping without settling is half-ownership,
        which is the state this method exists to prevent.
        """
        stop_error: BaseException | None = None
        try:
            await _shielded_cleanup(self._stop(None))
        except BaseException as stop_failure:  # ruff: ignore[blind-except] - held, not handled: settling must still run
            stop_error = stop_failure
        try:
            await _shielded_cleanup(self._settled(None))
        except BaseException as settle_failure:  # ruff: ignore[blind-except] - held, not handled: the stop's failure takes precedence
            if stop_error is None:
                stop_error = settle_failure
        if stop_error is not None and exc is None:
            raise stop_error
        # Otherwise the block is already failing. That error is the one the
        # caller has to see, so teardown's own failure is left on the
        # observation stream rather than allowed to take its place.

    def _execution(self, deadline: float | None) -> _SubprocessExecution:
        """Return the bundle this run is waited with, carrying *deadline*.

        Returns
        -------
        _SubprocessExecution
            The run's own bundle with its timeout replaced. The standing
            deadline was stripped at spawn, so this copy is the only place a
            wait's deadline comes from.
        """
        return dc.replace(self._run.execution, timeout=deadline)

    async def _stop(self, grace: float | None) -> None:
        """Escalate the child to a stop; idempotent, and never waits to settle."""
        await _terminate_shielded(
            self._run.process,
            self._run.execution.ctx.cancel_grace if grace is None else grace,
        )

    async def _settled(self, deadline: float | None) -> CommandResult:
        """Return the run's outcome, settling it if nobody has yet.

        The settlement task completes even when a deadline ended the run, so
        this awaits an outcome rather than an error. A deadline is reported to
        a caller that both asked for one and was still waiting when it elapsed;
        everyone else — a later wait, a stop, a teardown — reads the outcome
        the child actually has, because it has already been stopped and reaped.

        Returns
        -------
        CommandResult
            The outcome of the one settlement, whichever caller started it and
            whichever route — an ordinary exit, a stop, a kill, or a deadline —
            produced it.

        Raises
        ------
        TimeoutExpired
            If this call's deadline is what ended the run. The outcome is
            recorded all the same, and readable from :attr:`result`.
        """  # ruff: ignore[docstring-extraneous-exception] - raised here from the settlement this wait drove
        settlement, was_waiting = await self._await_settlement(deadline)
        expiry = self._expiry_for(settlement, deadline, was_waiting=was_waiting)
        if expiry is not None:
            raise expiry
        return settlement.result

    @staticmethod
    def _expiry_for(
        settlement: _HandleSettlement,
        deadline: float | None,
        *,
        was_waiting: bool,
    ) -> TimeoutExpired | None:
        """Return the expiry this call owes its caller, if it owes one.

        All three conditions have to hold. The settlement must have ended on a
        deadline, because otherwise there is no expiry to report. The caller
        must have supplied a deadline, because a wait with none cannot be said
        to have run out of time. And the caller must have been waiting on the
        live settlement rather than reading a record, because a caller who
        arrived afterwards is looking at a child that is already gone.

        Returns
        -------
        TimeoutExpired | None
            The expiry to raise, or ``None`` when this call should read the
            outcome instead.
        """
        if not was_waiting or deadline is None:
            return None
        return settlement.deadline_error

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
            The settlement's outcome, and whether this call awaited a
            settlement that was still in progress.
        """
        async with self._lock:
            settlement = self._settled_run
            if settlement is not None:
                return settlement, False
            settling = self._settling
            if settling is None:
                settling = asyncio.ensure_future(self._settle(deadline))
                self._settling = settling
        # Shielded so a cancelled waiter does not take the settlement with it:
        # the task is the handle's, and the next waiter still gets its outcome.
        awaited = await asyncio.shield(settling)
        async with self._lock:
            self._settled_run = awaited
        return awaited, True

    async def _settle(self, deadline: float | None) -> _HandleSettlement:
        """Drive the run to its end once, and publish what it produced.

        The run's own tasks are reconciled by :func:`_settle_handle_run`, which
        also carries the deadline; what remains here is the command-level
        framing — closing the presentation-sink session and draining the tasks
        an observer scheduled — and recording the outcome every later caller
        reads.

        A deadline does not raise from here. The outcome is recorded and the
        task completes, so the error describing the expiry reaches only the
        caller whose clock produced it rather than being re-raised at every
        operation that touches the handle afterwards.

        Returns
        -------
        _HandleSettlement
            The command's outcome, also stored for :attr:`result`, together
            with the deadline error when one ended the wait.
        """
        execution = self._execution(deadline)
        try:
            settlement = await _settle_handle_run(self._run, execution)
        except BaseException as run_error:
            await _finalize_command_run_failure(
                execution,
                self._run.tracking,
                None,
                run_error,
            )
            raise
        await _finalize_command_run_success(
            execution,
            self._run.tracking,
            settlement.result,
        )
        self._result = settlement.result
        return settlement
