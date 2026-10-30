"""Spawning a child whose lifetime is transferred out of cuprum's ownership.

The managed execution paths reap what they spawn: ``run()`` waits for its child
before returning, and a ``RunningCommand`` settles the child it started. A
detached launch deliberately does neither, so it cannot use the process object
those paths use. CPython's ``subprocess.Popen`` carries a finalizer that reports
a still-running child as a ``ResourceWarning`` and enrols the object in
``subprocess._active``, which keeps it alive until interpreter shutdown and
then waits on it. Both behaviours are that ownership, and the whole point of
this module is to give it away.

:class:`_DetachedPopen` keeps everything ``Popen`` does for the spawn itself —
the fork-and-exec, the exec-failure report, and the parent-side descriptor
handling — and removes only the finalizer. Once the caller has taken the PID,
dropping the object releases cuprum's copy of the handle and leaves the child
running.
"""

from __future__ import annotations

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
    -- and appends the object to ``subprocess._active``, which waits for the
    child at interpreter shutdown. Neither terminates the child, but both act as
    if the launch still owned it, which by then it does not.
    """

    def __del__(self) -> None:
        """Leave the child alone; its lifetime is no longer ours."""


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
    """  # ruff: ignore[docstring-extraneous-exception] - OSError propagates from Popen.
    return _DetachedPopen(
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


__all__ = ["_DetachedPopen", "_spawn_detached"]
