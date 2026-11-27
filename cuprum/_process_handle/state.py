"""The owned handle to a running command whose output is decoded text.

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

The lifecycle that is the same for every owned handle — control operations,
the stop lock, and the shielded teardown — lives in
:mod:`cuprum._process_handle.base`. This module adds the text result contract:
``wait`` and ``result`` are typed to ``CommandResult``.

Settlement is the other half, and it lives in
:mod:`cuprum._process_handle.settlement`: one task drives the run to its end,
every operation that needs an outcome awaits that same task, and the rule for
who hears about a deadline — and for what is recorded either way — is stated
once, there.
"""

from __future__ import annotations

import typing as typ

from cuprum._process_handle.base import _OwnedCommandHandle
from cuprum._process_handle.settlement import _TEXT_SETTLEMENT, _Settlement

if typ.TYPE_CHECKING:
    from cuprum._process_handle.tasks import _HandleRun
    from cuprum.sh import CommandResult

__all__ = ["RunningCommand"]


class RunningCommand(_OwnedCommandHandle["CommandResult"]):
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

    __slots__ = ()

    def __init__(self, run: _HandleRun) -> None:
        """Take ownership of a spawned child and its execution record."""
        super().__init__(run, _Settlement(run, _TEXT_SETTLEMENT))

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
