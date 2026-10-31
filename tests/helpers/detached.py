"""Reading what a detached launcher and its worker left behind.

Detachment is a claim about two processes, so the only honest way to test it is
with two: a launcher process starts a worker through cuprum and exits, and the
worker keeps running. Nothing the worker reports can travel over its standard
streams — a detached launch disconnects all three by design — so both processes
report through JSON files in the directory the caller names.

The programs themselves, and the code that starts the launcher, live in
:mod:`tests.helpers.detached_process`. This module reads their records, waits for
them to appear, and stops a worker left behind by a case.
"""

from __future__ import annotations

import dataclasses as dc
import json
import os
import signal
import time
import typing as typ

from cuprum import _detached_launch
from tests.helpers.process_state import process_state

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    import pytest

__all__ = [
    "FAKE_PID",
    "FakeProcess",
    "Receipt",
    "SpawnCall",
    "WorkerReport",
    "is_running",
    "read_heartbeat",
    "read_receipt",
    "read_worker_report",
    "record_abandonment",
    "record_spawns",
    "stop_group",
    "wait_for_file",
    "wait_for_heartbeats",
]

#: A PID no test has started. It is plausible enough that the kernel could have
#: assigned it (the default ``pid_max`` exceeds it), so ``record_spawns`` also
#: replaces the abandonment path with a guard: a launch that reaches it fails
#: the case instead of signalling a stranger's process group.
FAKE_PID = 2_000_000


@dc.dataclass(frozen=True, slots=True)
class SpawnCall:
    """The arguments a launch handed to the spawn seam."""

    argv: tuple[str, ...]
    env: cabc.Mapping[str, str] | None
    cwd: str | None
    start_new_session: bool


class FakeProcess:
    """The part of a ``Popen`` object a launch reads after spawning."""

    __slots__ = ("pid",)

    def __init__(self, *, pid: int = FAKE_PID) -> None:
        """Store the PID this stand-in reports."""
        self.pid = pid


def record_spawns(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pid: int = FAKE_PID,
) -> list[SpawnCall]:
    """Replace the spawn seam with one that records and reports *pid*.

    Returns
    -------
    list[SpawnCall]
        The calls the launch makes, in order. Empty means no child was started.
    """
    calls: list[SpawnCall] = []

    def recording_spawn(
        argv: cabc.Sequence[str],
        *,
        env: cabc.Mapping[str, str] | None,
        cwd: str | None,
        start_new_session: bool,
    ) -> FakeProcess:
        """Record the spawn inputs and hand back a PID."""
        calls.append(SpawnCall(tuple(argv), env, cwd, start_new_session))
        return FakeProcess(pid=pid)

    def refuse_abandonment(pid: int) -> None:
        """Fail the case rather than signal a fake PID."""
        message = f"unexpected abandonment of fake PID {pid}"
        raise AssertionError(message)

    monkeypatch.setattr(_detached_launch, "_spawn_detached", recording_spawn)
    # ``FAKE_PID`` can name a live process on a host whose ``pid_max`` exceeds
    # it, so the default abandonment path must never run against a recorded
    # spawn. A case that expects abandonment replaces this guard with
    # ``record_abandonment``.
    monkeypatch.setattr(
        _detached_launch,
        "_abandon_detached",
        refuse_abandonment,
    )
    return calls


def record_abandonment(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the abandonment path with a recorder.

    Returns
    -------
    list[int]
        The PIDs the launch abandoned, in order.
    """
    abandoned: list[int] = []
    monkeypatch.setattr(_detached_launch, "_abandon_detached", abandoned.append)
    return abandoned


#: How often a wait re-checks the fact it is waiting for.
_POLL_SECONDS = 0.01


@dc.dataclass(frozen=True, slots=True)
class Receipt:
    """The launcher's account of the launch it made.

    Attributes
    ----------
    pid:
        The worker's PID as the launcher's receipt named it.
    program:
        The catalogued program the command named.
    executable:
        The file that was executed.
    entry_point:
        ``"async"`` when the launcher awaited ``launch_detached`` on a loop,
        ``"sync"`` when it called ``launch_detached_sync``. It lets a case prove
        which spelling actually ran rather than inferring it from the launch.
    """

    pid: int
    program: str
    executable: str
    entry_point: str


@dc.dataclass(frozen=True, slots=True)
class WorkerReport:
    """What the worker could see about itself once it was running."""

    pid: int
    sid: int
    pgid: int
    argv: tuple[str, ...]
    cwd: str
    env: cabc.Mapping[str, str]
    stdin_eof: bool
    fd_targets: cabc.Mapping[str, str | None]


def read_receipt(path: pathlib.Path) -> Receipt:
    """Read the receipt the launcher wrote to *path*."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return Receipt(
        pid=typ.cast("int", payload["pid"]),
        program=typ.cast("str", payload["program"]),
        executable=typ.cast("str", payload["executable"]),
        entry_point=typ.cast("str", payload["entry_point"]),
    )


def wait_for_file(path: pathlib.Path, *, timeout: float = 30.0) -> None:
    """Block until *path* exists, failing with the path when it never does."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(_POLL_SECONDS)
    message = f"timed out after {timeout}s waiting for {path}"
    raise AssertionError(message)


def read_worker_report(path: pathlib.Path) -> WorkerReport:
    """Read the report the worker wrote to *path*."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return WorkerReport(
        pid=typ.cast("int", payload["pid"]),
        sid=typ.cast("int", payload["sid"]),
        pgid=typ.cast("int", payload["pgid"]),
        argv=tuple(typ.cast("list[str]", payload["argv"])),
        cwd=typ.cast("str", payload["cwd"]),
        env=typ.cast("dict[str, str]", payload["env"]),
        stdin_eof=bool(payload["stdin_eof"]),
        fd_targets=typ.cast("dict[str, str | None]", payload["fd_targets"]),
    )


def read_heartbeat(path: pathlib.Path) -> int:
    """Return the worker's latest beat count, or zero before its first beat."""
    try:
        return int(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return 0


def wait_for_heartbeats(
    path: pathlib.Path,
    *,
    after: int,
    count: int = 2,
    timeout: float = 15.0,
) -> int:
    """Wait for *count* beats beyond *after* and return the latest count.

    Returns
    -------
    int
        The beat count last published by the worker.

    Raises
    ------
    AssertionError
        If the worker does not publish that many beats within *timeout*.
    """
    deadline = time.monotonic() + timeout
    beats = after
    while time.monotonic() < deadline:
        beats = read_heartbeat(path)
        if beats >= after + count:
            return beats
        time.sleep(_POLL_SECONDS)
    message = (
        f"timed out after {timeout}s waiting for {count} beats beyond {after}; "
        f"the worker last published {beats}"
    )
    raise AssertionError(message)


def is_running(pid: int) -> bool:
    """Report whether *pid* names a live, unreaped process."""
    state = process_state(pid)
    if state.available:
        return not state.exited
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def stop_group(pid: int, *, timeout: float = 10.0) -> None:
    """Signal *pid*'s group and wait for it to stop running.

    The worker is not this process's child, so it cannot be reaped from here;
    waiting for it to disappear from the process table is the strongest
    available confirmation. ``SIGKILL`` is used because the worker ignores
    nothing and the case wants it gone, not asked to leave.

    Raises
    ------
    AssertionError
        If the worker is still running when the timeout expires.
    """
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running(pid):
            return
        time.sleep(_POLL_SECONDS)
    message = f"worker {pid} was still running {timeout}s after SIGKILL"
    raise AssertionError(message)
