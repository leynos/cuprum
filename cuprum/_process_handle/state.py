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

Settlement is the other half, and it lives in
:mod:`cuprum._process_handle.settlement`: one task drives the run to its end,
every operation that needs an outcome awaits that same task, and the rule for
who hears about a deadline — and for what is recorded either way — is stated
once, there. This module is the handle's surface: what a caller can ask of it,
and what each answer guarantees.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._process_handle.control import (
    _deliver_signal,
    _kill_shielded,
    _terminate_shielded,
)
from cuprum._process_handle.settlement import _Settlement
from cuprum._process_lifecycle import _shielded_cleanup

if typ.TYPE_CHECKING:
    import types

    from cuprum._process_handle.tasks import _HandleRun
    from cuprum.events import ExecId
    from cuprum.sh import CommandResult

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

    __slots__ = ("_run", "_settlement")

    def __init__(self, run: _HandleRun) -> None:
        """Take ownership of a spawned child and its execution record."""
        self._run = run
        self._settlement = _Settlement(run)

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
        return self._settlement.result

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
        return await self._settlement.outcome(self._default_deadline(timeout))

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
        return await self._settlement.outcome()

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
        return await self._settlement.outcome()

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
        succeeded — otherwise it would replace the error that caused the exit.

        Cancellation is the exception to that rule, and the reason is not
        stylistic. ``_shielded_cleanup`` finishes the teardown and then
        re-raises the ``CancelledError`` it absorbed, so holding it as an
        ordinary teardown failure and swallowing it because the block already
        failed would lose the cancel request: the caller handles its own error
        and keeps going, while the task that was told to stop never sees the
        cancellation. From 3.11 on it stays marked cancelled with no matching
        ``CancelledError``, and an enclosing ``asyncio.timeout`` or task-group
        shutdown then waits on, or misreports, a task that was never actually
        cancelled. So cancellation always propagates; only ordinary teardown
        failures defer to the block's error.

        Settlement is attempted even when the stop did not complete cleanly,
        and that ordering is deliberate. The two steps are separate *try*
        blocks rather than one, because a cancellation arriving during the
        stop would otherwise skip the settle entirely: the child would be
        reaped but no result recorded, leaving a handle whose :attr:`result`
        stays ``None`` forever. Reaping without settling is half-ownership,
        which is the state this method exists to prevent.
        """
        stop_error: BaseException | None = None
        cancellation: asyncio.CancelledError | None = None
        try:
            await _shielded_cleanup(self._stop(None))
        except asyncio.CancelledError as cancelled:
            cancellation = cancelled
        except BaseException as stop_failure:  # ruff: ignore[blind-except] - held, not handled: settling must still run
            stop_error = stop_failure
        try:
            await _shielded_cleanup(self._settlement.outcome())
        except asyncio.CancelledError as cancelled:
            # The first cancellation is the one the caller is owed: it is the
            # request that arrived, and a second is a repeat of it.
            cancellation = cancellation or cancelled
        except BaseException as settle_failure:  # ruff: ignore[blind-except] - held, not handled: the stop's failure takes precedence
            if stop_error is None:
                stop_error = settle_failure
        if cancellation is not None:
            raise cancellation
        if stop_error is not None and exc is None:
            raise stop_error
        # Otherwise the block is already failing. That error is the one the
        # caller has to see, so an ordinary teardown failure is dropped rather
        # than allowed to take its place.

    async def _stop(self, grace: float | None) -> None:
        """Escalate the child to a stop; idempotent, and never waits to settle."""
        await _terminate_shielded(
            self._run.process,
            self._run.execution.ctx.cancel_grace if grace is None else grace,
        )
