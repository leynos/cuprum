"""Post-spawn ownership at the public boundary on the asyncio fallback.

The startup reclaim is exercised elsewhere against the helper it lives in. That
leaves one question open: does the *public* run actually reach it when the
fallback backend spawned the child? The fallback hands back asyncio's own
subprocess, whose output pipes belong to transports that close only once their
pipe reports end-of-file, and a descendant that inherited the pipe prevents
that end-of-file forever. So the reclaim's own close is the only thing that
releases the parent-side descriptors, and this case drives it through
``SafeCmd.run()`` rather than through the helper.

The failure is a raising ``start`` observe hook, which is the one post-spawn
step that runs before anything is handed back to the caller. The child spawns a
descendant that inherits stdout and stderr and asks for its own session, so the
group teardown cannot reach it and both pipes stay open. The hook waits for the
descendant's pid to be recorded before it raises, so the descendant is provably
alive when the reclaim begins rather than racing it.

Everything asserted here is a POSIX signal: ``os.kill(pid, 0)`` probes liveness
and ``SIGKILL`` cleans up the reparented descendant.
"""

from __future__ import annotations

import asyncio
import time
import typing as typ

import pytest

from cuprum import ScopeConfig, _subprocess_spawn, _wait4_spawn, scoped, sh
from cuprum._pipeline_stream_fds import _extract_stream_fd
from cuprum.sh import RunOutputOptions
from cuprum.unittests._start_ownership_support import (
    _python_catalogue,
    _StartHookError,
)
from cuprum.unittests._startup_abandon_support import (
    _DESCENDANT_SCRIPT,
    _is_fd_open,
    _is_running,
    _kill_descendant,
    _posix_only,
    _recorded_pid,
)
from tests.helpers.timeouts import wait_for_process_death

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.catalogue import ProgramCatalogue
    from cuprum.events import ExecEvent


#: How long the descendant waits before exiting. Sized well past the reclaim
#: bound below, so a reclaim that waited for it would miss that bound.
_DESCENDANT_LIFETIME_S = 30

#: How long the failing hook waits for the child to record its descendant's
#: pid. The child only starts an interpreter and forks, but this host is shared.
_PID_FILE_WAIT_S = 10.0

#: Upper bound on the reclaim. The discard is bounded by the capture EOF grace
#: per run, so a healthy reclaim settles inside a second; anything approaching
#: the descendant's lifetime means the reclaim waited for an exit that never
#: comes.
_RECLAIM_BOUND_S = 5.0


def _command(
    *argv: str,
) -> tuple[sh.SafeCmd, ProgramCatalogue]:
    """Build a Python command and the allowlist catalogue that admits it."""
    program, catalogue = _python_catalogue("direct-start-fallback")
    return sh.make(program, catalogue=catalogue)(*argv), catalogue


def _wait_for_file(path: pathlib.Path, timeout_s: float) -> None:
    """Block until *path* exists, so the descendant is known to be running.

    A synchronous poll is deliberate: this runs inside an observe hook, which
    must not await. The child is a separate process, so its progress does not
    depend on this thread yielding.

    Raises
    ------
    AssertionError
        If *path* does not appear within *timeout_s*.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    msg = f"{path} was not written within {timeout_s}s"
    raise AssertionError(msg)


def _fail_start_once_the_descendant_is_recorded(
    events: list[ExecEvent],
    pid_file: pathlib.Path,
) -> cabc.Callable[[ExecEvent], None]:
    """Return a hook that records events and fails after the descendant exists."""

    def hook(event: ExecEvent) -> None:
        """Wait for the recorded pid on ``start``, then fail."""
        events.append(event)
        if event.phase == "start":
            _wait_for_file(pid_file, _PID_FILE_WAIT_S)
            raise _StartHookError

    return hook


def _record_spawns(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[asyncio.subprocess.Process, tuple[int | None, ...]]]:
    """Capture each child the public run spawns, without changing the spawn.

    The parent-side descriptors are read here, at spawn time, because the
    reclaim closes them: by the time the caller sees the hook's error the mute
    stream no longer discloses the number this case has to watch, so it must be
    taken while the pipe is certainly open.

    Returns
    -------
    list[tuple[asyncio.subprocess.Process, tuple[int | None, ...]]]
        Each spawned child with the descriptors its output pipes had when it
        was spawned, in spawn order. The case reads the child it expects, so it
        never has to guess which is which.
    """
    original = _subprocess_spawn._spawn_subprocess
    spawned: list[tuple[asyncio.subprocess.Process, tuple[int | None, ...]]] = []

    async def recording(
        execution: _SubprocessExecution,
    ) -> asyncio.subprocess.Process:
        """Spawn as usual, keeping the child and its live descriptors."""
        process = await original(execution)
        spawned.append((
            process,
            tuple(
                _extract_stream_fd(stream)
                for stream in (process.stdout, process.stderr)
            ),
        ))
        return process

    monkeypatch.setattr(_subprocess_spawn, "_spawn_subprocess", recording)
    return spawned


def _select_the_asyncio_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the run take the backend whose pipes its transports own."""
    monkeypatch.setattr(
        _wait4_spawn,
        "wait4_resource_measurement_available",
        lambda: False,
    )


class _Effects(typ.NamedTuple):
    """What a failed run and its reclaim left observable."""

    pid: int
    descriptors: tuple[int | None, ...]
    descendant_alive: bool
    bounded: bool
    reaped: bool


async def _run_until_the_hook_fails(
    command: sh.SafeCmd,
    spawned: list[tuple[asyncio.subprocess.Process, tuple[int | None, ...]]],
    pid_file: pathlib.Path,
) -> _Effects:
    """Run the command, and read the reclaim's effects inside the loop.

    The descriptor and liveness readings are taken before this coroutine
    returns, because the event loop is torn down immediately afterwards: a
    transport that had merely been asked to close would never get its
    callback, so reading here is what makes "closed" mean closed.

    Returns
    -------
    _Effects
        The child's pid and output-pipe descriptors, whether the descendant is
        still alive, whether the reclaim stayed inside its bound, and whether
        the child was reaped.
    """
    started_at = time.monotonic()
    with pytest.raises(_StartHookError):
        await command.run(output=RunOutputOptions())
    elapsed = time.monotonic() - started_at
    assert len(spawned) == 1, f"expected one spawn, got {len(spawned)}"
    process, descriptors = spawned[0]
    descendant_pid = _recorded_pid(pid_file)
    assert descendant_pid is not None, "the hook must have seen the pid file"
    return _Effects(
        pid=process.pid,
        descriptors=descriptors,
        descendant_alive=_is_running(descendant_pid),
        bounded=elapsed < _RECLAIM_BOUND_S,
        reaped=process.returncode is not None,
    )


def _assert_the_reclaim_released_everything(effects: _Effects) -> None:
    """Assert the child was reaped and the pipes released, inside the bound."""
    assert effects.reaped, (
        "the direct child must be reaped by the time the hook's error "
        "reaches the caller"
    )
    wait_for_process_death(effects.pid, context="a failed start hook on the fallback")
    assert effects.bounded, (
        f"the reclaim took longer than {_RECLAIM_BOUND_S}s; it must be "
        "bounded by the end-of-file grace rather than wait for the "
        "descendant to exit"
    )
    assert effects.descendant_alive, (
        "the descendant must still hold the pipes when the descriptors are "
        "read, or this case proves nothing about releasing them"
    )
    for fd in effects.descriptors:
        assert fd is not None, (
            "each captured stream must have a parent-side pipe whose "
            "descriptor this case can watch"
        )
        assert not _is_fd_open(fd), (
            f"descriptor {fd} must be closed before the event loop is torn "
            "down, even though no end-of-file arrived"
        )


@_posix_only
def test_public_run_releases_the_fallback_pipes_a_descendant_holds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """A public run that fails after its spawn still releases its pipes.

    The run is driven through ``SafeCmd.run()`` with an output capture, so the
    child has real parent-side pipes, and with the fallback selected, so those
    pipes belong to asyncio transports. A ``start`` hook then raises, which is
    the first post-spawn step and the one no caller can guard. By the time the
    hook's error reaches the caller, the direct child must be reaped, the
    parent-side descriptors must be closed, the descendant must still be alive
    (it is what kept them open), and the reclaim must have finished inside its
    bound rather than waited for that descendant.
    """
    pid_file = tmp_path / "descendant.pid"
    command, catalogue = _command(
        "-c",
        _DESCENDANT_SCRIPT,
        str(pid_file),
        str(_DESCENDANT_LIFETIME_S),
    )
    events: list[ExecEvent] = []
    _select_the_asyncio_fallback(monkeypatch)
    spawned = _record_spawns(monkeypatch)

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(_fail_start_once_the_descendant_is_recorded(events, pid_file)),
    ):
        effects = asyncio.run(_run_until_the_hook_fails(command, spawned, pid_file))

    try:
        _assert_the_reclaim_released_everything(effects)
    finally:
        _kill_descendant(_recorded_pid(pid_file))
