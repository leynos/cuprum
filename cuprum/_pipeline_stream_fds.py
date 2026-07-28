"""File-descriptor and transport controls for native pipeline pumping.

The Rust inter-stage pump temporarily owns asyncio's pipe descriptors.  The
blocking-mode guard and paused-reader scope make the two reversible parts of
that hand-off independently fault-injectable.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import os
import typing as typ

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc


def _fd_from_transport(transport: object | None) -> int | None:
    """Extract a raw FD via ``transport.get_extra_info('pipe').fileno()``."""
    get_extra = getattr(transport, "get_extra_info", None)
    if get_extra is None:
        return None
    pipe: object | None = get_extra("pipe")
    fileno = getattr(pipe, "fileno", None) if pipe is not None else None
    if fileno is None:
        return None
    try:
        return int(fileno())
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _extract_stream_fd(
    stream: asyncio.StreamReader | asyncio.StreamWriter | None,
) -> int | None:
    """Extract a raw FD from an asyncio stream via its transport."""
    if stream is None:
        return None
    transport = getattr(stream, "transport", None)
    if transport is None:
        transport = getattr(stream, "_transport", None)
    return _fd_from_transport(transport)


def _pause_reader_transport(
    reader: asyncio.StreamReader,
) -> cabc.Callable[[], None] | None:
    """Pause reader transport callbacks while Rust pump owns the raw FD."""
    transport = getattr(reader, "transport", None)
    if transport is None:
        transport = getattr(reader, "_transport", None)
    pause_reading = getattr(transport, "pause_reading", None)
    resume_reading = getattr(transport, "resume_reading", None)
    if not callable(pause_reading) or not callable(resume_reading):
        return None
    try:
        pause_reading()
    except (RuntimeError, OSError):
        return None

    def _resume() -> None:
        """Resume the paused reader transport, ignoring teardown errors."""
        with contextlib.suppress(RuntimeError, OSError):
            resume_reading()

    return _resume


def _set_stream_fds_blocking(*, reader_fd: int, writer_fd: int) -> tuple[bool, bool]:
    """Switch pipe FDs to blocking mode and return their prior state."""
    reader_changed = False
    try:
        reader_was_blocking = os.get_blocking(reader_fd)
        writer_was_blocking = os.get_blocking(writer_fd)
        if not reader_was_blocking:
            os.set_blocking(reader_fd, True)
            reader_changed = True
        if not writer_was_blocking:
            os.set_blocking(writer_fd, True)
    except (OSError, ValueError):
        if reader_changed:
            with contextlib.suppress(OSError, ValueError):
                os.set_blocking(reader_fd, reader_was_blocking)
        raise
    return reader_was_blocking, writer_was_blocking


def _restore_stream_fd_blocking(
    *,
    reader_fd: int,
    writer_fd: int,
    reader_was_blocking: bool,
    writer_was_blocking: bool,
) -> None:
    """Restore pipe FD blocking mode captured before Rust pumping."""
    with contextlib.suppress(OSError, ValueError):
        os.set_blocking(reader_fd, reader_was_blocking)
    with contextlib.suppress(OSError, ValueError):
        os.set_blocking(writer_fd, writer_was_blocking)


@dc.dataclass(frozen=True, slots=True)
class _BlockingModeGuard:
    """Prior blocking modes for a reader/writer descriptor pair."""

    reader_fd: int
    writer_fd: int
    reader_was_blocking: bool
    writer_was_blocking: bool

    @classmethod
    def engage(cls, *, reader_fd: int, writer_fd: int) -> _BlockingModeGuard:
        """Set both descriptors blocking and capture the modes to restore."""
        reader_was_blocking, writer_was_blocking = _set_stream_fds_blocking(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
        )
        return cls(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            reader_was_blocking=reader_was_blocking,
            writer_was_blocking=writer_was_blocking,
        )

    def restore(self) -> None:
        """Restore the modes captured by :meth:`engage`."""
        _restore_stream_fd_blocking(
            reader_fd=self.reader_fd,
            writer_fd=self.writer_fd,
            reader_was_blocking=self.reader_was_blocking,
            writer_was_blocking=self.writer_was_blocking,
        )


@contextlib.contextmanager
def _paused_reader(reader: asyncio.StreamReader) -> cabc.Iterator[None]:
    """Pause ``reader`` for the block and always resume it on exit."""
    resume = _pause_reader_transport(reader)
    try:
        yield
    finally:
        _resume_reader_transport(resume)


def _resume_reader_transport(
    resume_reader: cabc.Callable[[], None] | None,
) -> None:
    """Resume a reader transport when it was paused for native pumping."""
    if resume_reader is not None:
        resume_reader()


def _close_rust_writer_fd(writer_fd: int) -> None:
    """Close a native-pump writer descriptor after its worker has settled."""
    with contextlib.suppress(OSError):
        os.close(writer_fd)
