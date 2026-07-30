"""Raw file-descriptor lifecycle for the Rust inter-stage pump.

When the Rust backend pumps one pipeline stage's stdout into the next stage's
stdin it takes over the raw pipe descriptors from asyncio for the duration of
the transfer. That hand-off has several partial-failure paths: extracting the
descriptor from an asyncio transport, pausing the reader transport so asyncio
does not race the Rust pump, and switching the descriptors to blocking mode
(then restoring their prior mode). This module isolates that lifecycle behind
two seams — the [`_BlockingModeGuard`][cuprum._pipeline_stream_fds._BlockingModeGuard]
FD-state object and the [`_paused_reader`][cuprum._pipeline_stream_fds._paused_reader]
context manager — so it can be fault-injected without a live pump.
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


@dc.dataclass(frozen=True, slots=True)
class _ReaderPause:
    """The outcome of trying to pause a reader transport.

    Three outcomes are deliberately distinguished, because two of them are
    safe to hand the raw descriptor to the Rust pump and one is not:

    - **paused** — callbacks are suspended and ``resume`` restores them;
    - **unsupported** — the transport exposes no pause/resume hooks, so there
      are no callbacks to race and nothing to resume;
    - **failed** — ``pause_reading()`` raised, so the descriptor's state is
      unknown: asyncio may still be reading it, or the pause may have
      half-applied and left nothing watching it. Handing it to the Rust pump
      could race that reader, so ``may_hand_off`` is ``False`` and the caller
      falls back to Python. A corrective resume has already been attempted at
      the failure site, so ``resume`` is ``None`` here too.
    """

    may_hand_off: bool
    resume: cabc.Callable[[], None] | None = None


def _pause_reader_transport(reader: asyncio.StreamReader) -> _ReaderPause:
    """Pause reader transport callbacks while the Rust pump owns the raw FD."""
    transport = getattr(reader, "transport", None)
    if transport is None:
        transport = getattr(reader, "_transport", None)
    pause_reading = getattr(transport, "pause_reading", None)
    resume_reading = getattr(transport, "resume_reading", None)
    if not callable(pause_reading) or not callable(resume_reading):
        # No callbacks exist to race the Rust pump, so the hand-off is safe
        # and there is nothing to resume afterwards.
        return _ReaderPause(may_hand_off=True)
    try:
        pause_reading()
    except (RuntimeError, OSError):
        # The pause did not complete, so asyncio may still consume from the
        # descriptor. Report that the hand-off is unsafe rather than silently
        # racing the reader.
        #
        # A transport can also half-apply the pause: asyncio's own transports
        # set their paused flag and drop the reader before the trailing debug
        # log, so a raise from that log leaves the descriptor unwatched. The
        # Python fallback reads the same StreamReader and would stall on it
        # forever, which is worse than the error we are already handling. Undo
        # the half-applied pause here rather than at block exit, since a
        # transport that never paused ignores resume_reading().
        with contextlib.suppress(RuntimeError, OSError):
            resume_reading()
        return _ReaderPause(may_hand_off=False)

    def _resume() -> None:
        """Resume the paused reader transport, ignoring teardown errors."""
        with contextlib.suppress(RuntimeError, OSError):
            resume_reading()

    return _ReaderPause(may_hand_off=True, resume=_resume)


def _set_stream_fds_blocking(*, reader_fd: int, writer_fd: int) -> tuple[bool, bool]:
    """Switch pipe FDs to blocking mode and return their prior state."""
    reader_was_blocking = os.get_blocking(reader_fd)
    writer_was_blocking = os.get_blocking(writer_fd)
    reader_changed = False
    try:
        if not reader_was_blocking:
            os.set_blocking(reader_fd, True)
            reader_changed = True
        if not writer_was_blocking:
            os.set_blocking(writer_fd, True)
    except OSError:
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


@dc.dataclass(slots=True)
class _BlockingModeGuard:
    """The prior blocking mode of a reader/writer FD pair, restorable on demand.

    This is the FD-state object behind the Rust pump's blocking lifecycle: it
    captures each descriptor's blocking mode when the pump takes over the raw
    FDs and restores it afterwards, so no descriptor is left in the temporary
    blocking mode the Rust pump requires.
    """

    reader_fd: int
    writer_fd: int
    reader_was_blocking: bool
    writer_was_blocking: bool

    @classmethod
    def engage(cls, *, reader_fd: int, writer_fd: int) -> _BlockingModeGuard:
        """Switch both FDs to blocking mode, capturing their prior state.

        Raises ``OSError`` — after rolling back any partial change so no
        descriptor is left switched — when either FD cannot be made blocking.
        """
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
        """Restore both FDs to the blocking mode captured by ``engage``."""
        _restore_stream_fd_blocking(
            reader_fd=self.reader_fd,
            writer_fd=self.writer_fd,
            reader_was_blocking=self.reader_was_blocking,
            writer_was_blocking=self.writer_was_blocking,
        )


@contextlib.contextmanager
def _paused_reader(reader: asyncio.StreamReader) -> cabc.Iterator[bool]:
    """Pause the reader transport for the block, resuming it on every exit.

    Wraps ``_pause_reader_transport`` so a completed pause is always undone —
    on normal return, exception, or cancellation. Exactly one resume ever
    fires per pause attempt: a transport with no pause/resume hooks needs
    none, and a ``pause_reading()`` that raised has already had its corrective
    resume attempted at the failure site, so neither is resumed again here.

    Yields ``True`` when the raw descriptor may be handed to the Rust pump and
    ``False`` when pausing failed, so the caller can fall back rather than race
    a reader that is still consuming the descriptor.
    """
    pause = _pause_reader_transport(reader)
    try:
        yield pause.may_hand_off
    finally:
        if pause.resume is not None:
            pause.resume()
