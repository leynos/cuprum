"""Acquire and release the descriptors used by the PTY benchmark sink."""

from __future__ import annotations

import contextlib
import dataclasses as dc
import io
import os
import pty
import termios
import typing as typ

from benchmarks._pty_writer import _CountingPtyWriter


class _ExpectedOutputRecorder(typ.Protocol):
    """Record output bytes with the PTY's newline-translation setting."""

    def __call__(self, data: bytes, *, translates_newlines: bool) -> None:
        """Record the accepted bytes after output processing."""


@dc.dataclass(frozen=True, slots=True)
class _PtyBlackholeResources:
    """Descriptors and streams acquired for one PTY sink context."""

    master_fd: int
    wake_read_fd: int
    wake_write_fd: int
    slave: typ.IO[str]
    raw_writer: _CountingPtyWriter


@dc.dataclass(slots=True)
class _PtySetupAttempt:
    """Track acquired resources until PTY setup is complete."""

    master_fd: int
    slave_fd: int
    wake_fds: tuple[int, int] | None = None
    slave: typ.IO[str] | None = None
    raw_writer: _CountingPtyWriter | None = None


def _open_pty_blackhole_resources(
    encoding: str,
    errors: str,
    on_bytes_written: _ExpectedOutputRecorder,
) -> _PtyBlackholeResources:
    """Open and configure the PTY stream, closing partial setup on failure."""
    master_fd, slave_fd = pty.openpty()
    attempt = _PtySetupAttempt(master_fd, slave_fd)
    try:
        os.set_blocking(attempt.master_fd, False)
        translates_newlines = _configure_output_processing(attempt.slave_fd)
        wake_read_fd, wake_write_fd = os.pipe()
        attempt.wake_fds = wake_read_fd, wake_write_fd
        raw_writer = _CountingPtyWriter(
            attempt.slave_fd,
            lambda data: on_bytes_written(
                data,
                translates_newlines=translates_newlines,
            ),
        )
        attempt.raw_writer = raw_writer
        slave = io.TextIOWrapper(
            io.BufferedWriter(raw_writer),
            encoding=encoding,
            errors=errors,
            line_buffering=True,
        )
        attempt.slave = slave
        return _PtyBlackholeResources(
            attempt.master_fd,
            wake_read_fd,
            wake_write_fd,
            slave,
            raw_writer,
        )
    except BaseException:
        _close_partial_setup(attempt)
        raise


def _configure_output_processing(slave_fd: int) -> bool:
    """Set predictable newline handling and report whether it expands LF."""
    attrs = termios.tcgetattr(slave_fd)
    onlcr = getattr(termios, "ONLCR", 0)
    output_flags = termios.OPOST | onlcr
    attrs[1] = output_flags
    termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
    return bool(output_flags & termios.OPOST and onlcr)


def _close_partial_setup(attempt: _PtySetupAttempt) -> None:
    """Close every descriptor that may have been acquired during setup."""
    if attempt.slave is not None:
        with contextlib.suppress(OSError):
            attempt.slave.close()
    if attempt.raw_writer is not None:
        with contextlib.suppress(OSError):
            attempt.raw_writer.close()
    elif attempt.slave is None:
        with contextlib.suppress(OSError):
            os.close(attempt.slave_fd)
    descriptors = (attempt.master_fd, *(attempt.wake_fds or ()))
    for fd in descriptors:
        with contextlib.suppress(OSError):
            os.close(fd)


def _close_unstarted_resources(resources: _PtyBlackholeResources) -> None:
    """Close acquired resources if the drainer thread could not start."""
    with contextlib.suppress(OSError):
        resources.slave.close()
    with contextlib.suppress(OSError):
        resources.raw_writer.close()
    for fd in (
        resources.master_fd,
        resources.wake_read_fd,
        resources.wake_write_fd,
    ):
        with contextlib.suppress(OSError):
            os.close(fd)
