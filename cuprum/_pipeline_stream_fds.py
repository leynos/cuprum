"""File-descriptor and transport controls for native pipeline pumping.

The Rust inter-stage pump temporarily owns asyncio's pipe descriptors.  The
blocking-mode guard and paused-reader scope make the two reversible parts of
that hand-off independently fault-injectable.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import logging
import os
import typing as typ

from cuprum.pump_events import RustPumpDeclineReason

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc

_LOGGER = logging.getLogger(__name__)

RUST_PUMP_TEARDOWN_FAILED_ACTION = "rust_pump_teardown_failed"
"""``cuprum_action`` for a Rust-pump teardown step that failed and was ignored."""


def _fd_from_transport(transport: object | None) -> int | None:
    """Extract a raw FD via ``transport.get_extra_info('pipe').fileno()``.

    Every attribute here is duck-typed, so each is checked for callability
    before use: a transport double, or an unusual implementation carrying a
    non-callable ``get_extra_info``, would otherwise raise ``TypeError`` from
    outside the ``try`` below. Nothing upstream catches that —
    ``_extract_stream_fd`` calls straight through — so it would surface as a
    crash where the contract is to decline the fast path and return ``None``.
    """
    get_extra = getattr(transport, "get_extra_info", None)
    if not callable(get_extra):
        return None
    pipe: object | None = get_extra("pipe")
    fileno = getattr(pipe, "fileno", None) if pipe is not None else None
    if not callable(fileno):
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


@dc.dataclass(frozen=True, slots=True, init=False)
class _ReaderPause:
    """The outcome of trying to pause a reader transport.

    A transport with no ``pause_reading`` hook has no callbacks to race. One
    with a pause hook but no ``resume_reading`` hook is unsafe to hand off: it
    cannot be paused and later returned to asyncio, so it must fall back.
    """

    may_hand_off: bool
    resume: cabc.Callable[[], None] | None
    decline_reason: RustPumpDeclineReason | None

    def __init__(
        self,
        may_hand_off: bool | None = None,
        resume: cabc.Callable[[], None] | None = None,
        decline_reason: RustPumpDeclineReason | None = None,
    ) -> None:
        """Record a pause outcome, deriving its verdict from a decline reason."""
        object.__setattr__(
            self,
            "may_hand_off",
            decline_reason is None if may_hand_off is None else may_hand_off,
        )
        object.__setattr__(self, "resume", resume)
        object.__setattr__(self, "decline_reason", decline_reason)


def _pause_reader_transport(
    reader: asyncio.StreamReader,
) -> _ReaderPause:
    """Pause reader transport callbacks while Rust pump owns the raw FD."""
    transport = getattr(reader, "transport", None)
    if transport is None:
        transport = getattr(reader, "_transport", None)
    pause_reading = getattr(transport, "pause_reading", None)
    resume_reading = getattr(transport, "resume_reading", None)
    if not callable(pause_reading):
        return _ReaderPause(may_hand_off=True)
    if not callable(resume_reading):
        return _ReaderPause(
            may_hand_off=False,
            decline_reason=RustPumpDeclineReason.READER_UNRESUMABLE,
        )
    try:
        pause_reading()
    except (RuntimeError, OSError):
        # A transport can mark itself paused before raising. Resume here so the
        # Python fallback never inherits a reader that has lost its callbacks.
        with contextlib.suppress(RuntimeError, OSError):
            resume_reading()
        return _ReaderPause(
            may_hand_off=False,
            decline_reason=RustPumpDeclineReason.READER_PAUSE_FAILED,
        )

    def _resume() -> None:
        """Resume the paused reader transport, recording teardown errors."""
        with _suppressed_teardown_failure(_LOGGER, "resume", RuntimeError, OSError):
            resume_reading()

    return _ReaderPause(may_hand_off=True, resume=_resume)


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
    with _suppressed_teardown_failure(_LOGGER, "restore_blocking", OSError, ValueError):
        os.set_blocking(reader_fd, reader_was_blocking)
    with _suppressed_teardown_failure(_LOGGER, "restore_blocking", OSError, ValueError):
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
def _paused_reader(reader: asyncio.StreamReader) -> cabc.Iterator[_ReaderPause]:
    """Pause ``reader`` for the block and report whether hand-off is safe.

    An unsupported or unresumable transport is never paused, and a failed pause
    is corrected at the failure site, so this scope resumes only a completed
    pause.
    """
    pause = _pause_reader_transport(reader)
    try:
        yield pause
    finally:
        _resume_reader_transport(pause.resume)


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

@contextlib.contextmanager
def _suppressed_teardown_failure(
    logger: logging.Logger,
    site: str,
    *errors: type[Exception],
) -> cabc.Iterator[None]:
    """Suppress a teardown failure at ``site``, recording it at DEBUG first.

    Suppresses exactly what ``contextlib.suppress(*errors)`` would; the only
    addition is the record. Each of these steps undoes something the Rust pump
    did to a descriptor, and a step that has quietly stopped working leaves the
    descriptor in a state nothing else reports — the same question
    ``_log_rust_pump_declined`` exists to answer, so it is answered the same
    way and at the same level.

    Only the site and the exception class reach the record. Both are drawn from
    closed sets, so an operator aggregating these cannot be handed a series per
    descriptor or per message.

    The record is best-effort by construction: these steps run during teardown,
    which includes cancellation unwinding, and a logging handler that raised
    would displace the ``CancelledError`` the caller is owed. The emission is
    therefore wrapped in its own suppression rather than being allowed to
    convert a suppressed teardown error into a raised logging one.
    """
    try:
        yield
    except errors as exc:
        with contextlib.suppress(Exception):
            logger.debug(
                "Rust pump teardown step %r failed and was ignored",
                site,
                extra={
                    "cuprum_action": RUST_PUMP_TEARDOWN_FAILED_ACTION,
                    "cuprum_site": site,
                    "cuprum_error_type": type(exc).__name__,
                    "cuprum_errno": getattr(exc, "errno", None),
                },
            )
