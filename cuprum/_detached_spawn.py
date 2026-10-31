"""Spawning a child whose lifetime is transferred out of cuprum's ownership.

The managed execution paths reap what they spawn: ``run()`` waits for its child
before returning, and a ``RunningCommand`` settles the child it started. A
detached launch deliberately does neither, so it cannot use the process object
those paths use. CPython's ``subprocess.Popen`` carries a finalizer that assumes
the object still owns its child: it reports a still-running child as a
``ResourceWarning``, polls the child and reaps it if it has already exited, and
appends the object to ``subprocess._active`` so a later ``Popen`` construction
polls it again. All three are that ownership, and the whole point of this module
is to give it away.

:class:`_DetachedPopen` keeps everything ``Popen`` does for the spawn itself —
the fork-and-exec, the exec-failure report, and the parent-side descriptor
handling — and removes only the finalizer. Once the caller has taken the PID,
dropping the object releases cuprum's copy of the handle and leaves the child
running.

A constructor that fails is a separate case. ``Popen`` can raise after the child
exists but before ``__init__`` returns — a ``KeyboardInterrupt`` delivered while
it waits for the exec-status pipe is the realistic example — and its own error
path closes descriptors without terminating that child. :func:`_spawn_detached`
therefore constructs the object in two steps, so it still holds a reference
when construction raises, and reclaims the child before re-raising.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import] - the argv is a validated command and shell=False is pinned below.
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _DetachedPopen(subprocess.Popen[bytes]):
    """A ``Popen`` whose object may be dropped without reclaiming the child.

    Only the finalizer is overridden. Spawning, the error-pipe report that
    turns a failed exec into an exception, and the closing of parent-side
    descriptors stay ``Popen``'s, so a detached launch fails on a missing or
    unexecutable file in exactly the way a managed spawn does.

    The no-op finalizer is the reason this class exists. ``Popen.__del__`` warns
    from a finalizer -- where the warning cannot be caught by the launching code
    -- polls the child and reaps it if it has already exited, and retains the
    object in ``subprocess._active`` until a later ``Popen`` construction polls
    it again. Those are terminal acts of ownership, and the launch has given the
    child away.
    """

    def __del__(self) -> None:
        """Leave the child alone; its lifetime is no longer ours."""


def _reclaim_partial_spawn(process: _DetachedPopen) -> None:
    """Kill and reap a child whose ``Popen`` construction failed after the spawn.

    ``Popen`` sets the child's PID as soon as the fork succeeds and then waits
    for the executable's start-up report. An exception raised during that wait
    -- ``KeyboardInterrupt`` from a signal handler is the realistic one -- is
    not the ordinary exec-failure path: ``Popen`` closes its descriptors and
    propagates, leaving the child running and no object for a caller to hold.

    The PID is only signalled once the child is confirmed to be this process's
    unreaped child. ``Popen`` reaps the child itself when it reports an ordinary
    exec failure, and by then the identifier may already name another process.
    """
    pid = process.pid
    if pid is None:
        return
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        # ``Popen``'s own failure path already reaped it, so the identifier may
        # name an unrelated process now.
        return
    if waited == pid:
        return
    # The child is still running. Prefer its group, but the child may not have
    # reached ``setsid`` yet, in which case no group with this identifier
    # exists and the child itself has to be signalled.
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, 0)


def _spawn_detached(
    argv: cabc.Sequence[str],
    *,
    env: cabc.Mapping[str, str] | None,
    cwd: str | None,
    start_new_session: bool,
) -> _DetachedPopen:
    """Spawn a fully disconnected child and return the handle to release.

    All three standard streams are ``/dev/null``, so the child reads end-of-file
    on stdin and has nowhere to write: it holds no pipe, descriptor, or transport
    the launching process could later be expected to service. ``start_new_session``
    is supplied by the caller rather than defaulted here, because the policy that
    decides it also refuses platforms that cannot honour it.

    The process object is built in two steps because the constructor is not
    exception-safe against its own child: if ``Popen.__init__`` raises after the
    fork, the partially constructed object is the only reference to a child that
    is still running. Keeping that reference lets :func:`_reclaim_partial_spawn`
    end the child before the exception leaves this function.

    Returns
    -------
    _DetachedPopen
        The process object. The caller takes its PID and drops it; dropping it
        neither waits for nor signals the child.

    Raises
    ------
    OSError
        If the executable cannot be found or started. ``Popen`` reports a failed
        exec through its error pipe, so a child that failed to start has already
        been reaped when this propagates.
    BaseException
        Any other failure while the process object is being constructed,
        including a ``KeyboardInterrupt`` delivered during the wait for the exec
        report. A child created before the failure is stopped and reaped first.
    """  # ruff: ignore[docstring-extraneous-exception] - Popen's failures propagate as they are.
    process = _DetachedPopen.__new__(_DetachedPopen)
    try:
        # Called on the class so a test double's own ``__init__`` still runs.
        _DetachedPopen.__init__(
            process,
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=start_new_session,
            shell=False,
            env=env,
            cwd=cwd,
        )
    except BaseException:
        _reclaim_partial_spawn(process)
        raise
    return process


__all__ = ["_DetachedPopen", "_spawn_detached"]
