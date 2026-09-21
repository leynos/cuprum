"""Linux process and pipe diagnostics for stalled subprocess tests."""

from __future__ import annotations

import dataclasses as dc
import os
import pathlib
import sys

__all__ = [
    "ChildPipe",
    "ProcessState",
    "child_pipes",
    "parent_write_fds",
    "process_state",
]


@dc.dataclass(frozen=True, slots=True)
class ProcessState:
    """Snapshot of a process's scheduler state from procfs."""

    pid: int
    state: str | None
    exited: bool
    wchan: str | None
    available: bool


@dc.dataclass(frozen=True, slots=True)
class ChildPipe:
    """A child pipe descriptor and its readable byte count when observable."""

    fd: int
    target: str
    pending_bytes: int | None


def _procfs_available() -> bool:
    """Return whether this host provides the Linux procfs interface."""
    return sys.platform == "linux" and pathlib.Path("/proc").is_dir()


def _unavailable_process(pid: int) -> ProcessState:
    """Return the stable record used when procfs cannot be consulted."""
    return ProcessState(pid, state=None, exited=False, wchan=None, available=False)


def _state_from_stat(stat: str) -> str | None:
    """Extract the process state after the final command-name delimiter."""
    closing_parenthesis = stat.rfind(")")
    if closing_parenthesis < 0:
        return None
    fields = stat[closing_parenthesis + 1 :].split()
    return None if not fields else fields[0]


def _read_wchan(pid: int) -> str | None:
    """Read a Linux process's wait channel without surfacing diagnostics errors."""
    try:
        value = pathlib.Path(f"/proc/{pid}/wchan").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def process_state(pid: int) -> ProcessState:
    """Return the available Linux process state for ``pid``.

    A missing procfs entry means that the process has exited or been reaped.
    On unsupported hosts, return an unavailable record rather than making a
    test diagnostic obscure the original stalled pipeline.

    Returns
    -------
    ProcessState
        The process state, including whether the process has exited and
        whether procfs made the observation available.
    """
    if not _procfs_available():
        return _unavailable_process(pid)
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return ProcessState(pid, state=None, exited=True, wchan=None, available=True)
    except OSError:
        return _unavailable_process(pid)
    state = _state_from_stat(stat)
    return ProcessState(
        pid,
        state=state,
        exited=state == "Z",
        wchan=_read_wchan(pid),
        available=True,
    )


def _pending_bytes(path: pathlib.Path) -> int | None:
    """Return readable bytes for a procfs descriptor, when its ioctl supports it."""
    try:
        import fcntl
        import struct
        import termios
    except ImportError:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        response = fcntl.ioctl(fd, termios.FIONREAD, struct.pack("I", 0))
        return int(struct.unpack("I", response)[0])
    except OSError:
        return None
    finally:
        os.close(fd)


def child_pipes(pid: int) -> tuple[ChildPipe, ...]:
    """Return observable pipe descriptors and pending bytes for a child process.

    Unsupported or inaccessible descriptors are retained with ``None`` for
    their byte count so that a single procfs race cannot hide the stall that
    triggered the diagnostic.

    Returns
    -------
    tuple[ChildPipe, ...]
        Every observable pipe descriptor, ordered by descriptor number.
    """
    if not _procfs_available():
        return ()
    directory = pathlib.Path(f"/proc/{pid}/fd")
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        return ()
    pipes: list[ChildPipe] = []
    for entry in entries:
        try:
            target = str(entry.readlink())
            fd = int(entry.name)
        except (OSError, ValueError):
            continue
        if target.startswith("pipe:["):
            pipes.append(ChildPipe(fd, target, _pending_bytes(entry)))
    return tuple(sorted(pipes, key=lambda pipe: pipe.fd))


def parent_write_fds(pipe_target: str) -> tuple[int, ...]:
    """Return this process's writable descriptors for a Linux pipe target."""
    if not _procfs_available():
        return ()
    try:
        import fcntl
    except ImportError:
        return ()
    try:
        entries = tuple(pathlib.Path("/proc/self/fd").iterdir())
    except OSError:
        return ()
    writers: list[int] = []
    for entry in entries:
        try:
            fd = int(entry.name)
            if str(entry.readlink()) != pipe_target:
                continue
            mode = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
        except (OSError, ValueError):
            continue
        if mode in {os.O_WRONLY, os.O_RDWR}:
            writers.append(fd)
    return tuple(sorted(writers))
