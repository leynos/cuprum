"""Launching a command whose lifetime leaves cuprum's ownership.

Most cuprum entry points keep the child: ``run()`` waits for it, and ``start()``
hands back a ``RunningCommand`` that settles it. ``launch_detached`` does the
opposite. It starts the command with all three standard streams disconnected and
a new POSIX session, returns a :class:`DetachedLaunch` receipt carrying the real
PID, and then takes no further part in the child's life.

The split is expressed by the API rather than by a flag. Dropping a
``RunningCommand`` never detaches anything, and no option on ``run()`` turns a
managed run into a detached one. Only this explicit entry point transfers
ownership, so a caller reading a run site can tell which contract applies.

The launch itself is one synchronous sequence — allowlist, platform check,
executable resolution, spawn, receipt — with no suspension point between the
spawn and the receipt. The async entry point exists so an async caller need not
wrap a blocking call, not to introduce concurrency. A cancellation can therefore
be delivered before the coroutine starts, in which case no child is created, or
after it returns, in which case the caller owns the worker; it cannot land
between the spawn and the receipt. The async entry point still yields once after
the transfer, so a cancellation requested during the launch is delivered there
and the worker is stopped before the cancellation is re-raised.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import os
import signal
import typing as typ

from cuprum._context_policy import _enforce_allowlist, _merge_env
from cuprum._detached_spawn import _spawn_detached
from cuprum._observability import _bound_executable, _resolve_executable_for
from cuprum._subprocess_context import (
    _cwd_arg,
    _execution_cwd,
    _ownership_spawn_kwargs,
)
from cuprum.program import (  # ruff: ignore[typing-only-first-party-import] - public annotations must resolve at runtime, where typing.get_type_hints reads them.
    Program,
)
from cuprum.sh.execution import ExecutionContext, ProcessGroupPolicy

if typ.TYPE_CHECKING:
    from cuprum.sh import SafeCmd


@dc.dataclass(frozen=True, slots=True)
class DetachedLaunch:
    """The receipt for a command whose lifetime the caller now owns.

    A receipt is returned only after the worker's ``exec`` succeeded: ``Popen``
    reports an exec failure through its error pipe, so a missing or
    unexecutable file raises instead of producing a receipt. That is the
    strongest start-up guarantee available here. The worker may still reject its
    arguments, fail its own initialization, or exit immediately afterwards, and
    a receipt says nothing about any of that; a caller that needs readiness must
    implement its own signal with the worker.

    Attributes
    ----------
    pid:
        The worker's process identifier. On POSIX the worker is also the leader
        of a new session and process group, so its group identifier and session
        identifier are both equal to this value.
    program:
        The catalogued program the command named.
    executable:
        The file that was executed: the bound path when an executable binding
        applied, otherwise ``str(program)``. It is also the ``argv[0]`` the
        worker received.
    """

    pid: int

    program: Program

    executable: str


def _abandon_detached(pid: int) -> None:
    """Kill and reap a worker whose receipt could not be built.

    The launch owns the child until the receipt exists. If constructing the
    receipt fails — an interpreter-shutdown signal or an injected cancellation is
    the realistic case — ownership has not been transferred, so the child is
    ended here rather than left running with nobody holding its PID.

    The child's whole group is signalled because the spawn made it a session
    leader; ``SIGKILL`` is used because this path cannot wait out a graceful
    stop. ``ProcessLookupError`` means the child has already exited and
    ``ChildProcessError`` means it has already been reaped; either is the state
    this function is trying to reach.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, 0)


def _launch_detached(
    cmd: SafeCmd,
    context: ExecutionContext | None,
) -> DetachedLaunch:
    """Launch *cmd* and hand its lifetime to the caller.

    Returns
    -------
    DetachedLaunch
        The receipt naming the worker's PID and the file that was executed.

    Raises
    ------
    PermissionError
        If the command is not allowed by the active scope. The check precedes the
        spawn, so a refused program never creates a child.
    ValueError
        If the platform cannot provide the session isolation detachment
        requires. The check also precedes the spawn.
    OSError
        If the executable cannot be found or started.
    """  # ruff: ignore[docstring-extraneous-exception] - exceptions propagate from the allowlist, platform guard, and spawn
    _enforce_allowlist(cmd)
    # The guard is what refuses a platform without POSIX sessions, and the
    # keyword it returns is the same one the spawn is given, so the check and
    # the spawn cannot disagree about the isolation that was requested.
    ownership = _ownership_spawn_kwargs(ProcessGroupPolicy.OWN_GROUP)
    ctx = context or ExecutionContext()
    resolved_path = _resolve_executable_for(cmd, cwd=_execution_cwd(ctx.cwd))
    executable = _bound_executable(cmd.program, resolved_path)
    process = _spawn_detached(
        (executable, *cmd.argv),
        env=_merge_env(ctx.env, ctx.env_mode),
        cwd=_cwd_arg(ctx.cwd),
        start_new_session=ownership["start_new_session"],
    )
    # The guard starts here, not at the receipt construction: an
    # interpreter-shutdown signal can be delivered at any bytecode boundary, and
    # once the spawn has returned a child exists that only this frame knows
    # about. Everything between that point and the caller holding the receipt is
    # therefore covered.
    try:
        pid = process.pid
        receipt = DetachedLaunch(pid=pid, program=cmd.program, executable=executable)
    except BaseException:
        _abandon_detached(process.pid)
        raise
    # The receipt now holds everything cuprum keeps about this child. Dropping
    # the process object releases its own copy of the handle; its finalizer has
    # been made a no-op, so this neither waits for the child nor warns about it.
    del process
    return receipt


class _DetachedLaunchEntryPoints:
    """Mixin contributing ``SafeCmd.launch_detached`` and its sync twin.

    ``__slots__`` is declared empty rather than omitted so the un-slotted mixin
    cannot donate a ``__dict__`` to the slotted dataclass that inherits it, for
    the same reason :class:`cuprum._process_handle.owned_start._OwnedStart`
    declares it.
    """

    __slots__ = ()

    async def launch_detached(
        self,
        *,
        context: ExecutionContext | None = None,
    ) -> DetachedLaunch:
        """Launch the command and transfer its lifetime to the caller.

        The allowlist, executable binding, argument vector, environment, and
        working directory are resolved exactly as for a managed run, so a
        detached command is the same command it would have been. What differs is
        what happens afterwards: the child is placed in its own POSIX session and
        process group, all three standard streams are connected to ``/dev/null``,
        and cuprum retains nothing but the PID in the returned receipt.

        The receipt means the worker's ``exec`` succeeded. It does not mean the
        worker is ready, or even still alive: a command that exits immediately
        after starting still produces one. Readiness is the caller's contract
        with the worker.

        Ownership transfers when this returns, and only then. A cancellation
        delivered before the coroutine starts means no child is created, and
        one delivered after the receipt reaches the caller arrives after the
        transfer. The launch itself has no suspension point, so cancellation
        cannot split the spawn from the receipt. The coroutine does yield once
        immediately after the transfer, which gives a cancellation requested
        while the launch was running somewhere to land: if it lands there the
        caller never sees the receipt, so the worker is stopped and re-raised
        rather than leaked. A caller therefore either holds a receipt or is left
        with no child.

        Termination and reaping are the caller's from that point on. On POSIX the
        worker is re-parented to the init process when the launching process
        exits, so a caller that wants it to stop must signal it (or its group)
        before then. A PID is only meaningful while the process it names is
        alive; once the worker has exited and been reaped, the identifier may be
        reused, so a retained receipt must not be used to signal a process the
        caller cannot first confirm is the original.

        Parameters
        ----------
        context : ExecutionContext | None, default=None
            Execution settings supplying the environment overlay, working
            directory, and executable-binding base. Capture, echo, timeout, and
            ``process_group`` have no effect: a detached launch never captures,
            never streams, and never supervises the child's lifetime.

        Returns
        -------
        DetachedLaunch
            The receipt naming the worker's PID and the file that was executed.

        Raises
        ------
        PermissionError
            If the command is not allowed by the active scope.
        ValueError
            If the platform cannot provide POSIX session isolation.
        OSError
            If the executable cannot be found or started.
        CancelledError
            If the launch is cancelled at the transfer boundary. The worker is
            stopped before the cancellation is re-raised.
        """  # ruff: ignore[docstring-extraneous-exception] - exceptions propagate from the shared launch core
        receipt = _launch_detached(typ.cast("SafeCmd", self), context)
        # A cancellation requested while the launch was running cannot be
        # delivered until this coroutine yields. Yielding once after the
        # transfer gives it a place to land: if it lands here the receipt never
        # reaches the caller, so cuprum still owns the worker and stops it
        # rather than losing it. A cancellation delivered after this point
        # reaches a caller that already holds the receipt and owns the worker.
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            _abandon_detached(receipt.pid)
            raise
        return receipt

    def launch_detached_sync(
        self,
        *,
        context: ExecutionContext | None = None,
    ) -> DetachedLaunch:
        """Launch the command without requiring an event loop.

        The synchronous spelling of :meth:`launch_detached`. It performs the
        same launch and returns the same receipt, and it is safe to call from
        either a synchronous context or a running event loop: the launch never
        awaits, so no loop has to be running and none is blocked beyond the
        spawn itself.

        Parameters
        ----------
        context : ExecutionContext | None, default=None
            Execution settings, as for :meth:`launch_detached`.

        Returns
        -------
        DetachedLaunch
            The receipt naming the worker's PID and the file that was executed.

        Raises
        ------
        PermissionError
            If the command is not allowed by the active scope.
        ValueError
            If the platform cannot provide POSIX session isolation.
        OSError
            If the executable cannot be found or started.
        """  # ruff: ignore[docstring-extraneous-exception] - exceptions propagate from the shared launch core
        return _launch_detached(typ.cast("SafeCmd", self), context)


__all__ = ["DetachedLaunch", "_DetachedLaunchEntryPoints", "_launch_detached"]
