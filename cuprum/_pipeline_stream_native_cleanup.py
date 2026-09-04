"""Own native-pump executor cleanup and deferred descriptor hand-back.

This module is the sole owner of descriptors duplicated for a Rust pump.  Its
completion callback closes worker and callback duplicates, restores blocking
mode, and resumes the paused reader only after native I/O has settled.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import functools
import logging
import os
import time
import typing as typ

from cuprum._pipeline_stream_cleanup_observation import (
    _log_native_pump_cleanup_completed,
    _log_native_pump_cleanup_deferred,
    _log_native_pump_cleanup_grace_expired,
    _log_native_pump_cleanup_started,
)
from cuprum._pipeline_stream_fds import (
    _BlockingModeGuard,
    _close_rust_reader_fd,
    _close_rust_state_fd,
    _close_rust_writer_fd,
    _resume_reader_transport,
    _suppressed_teardown_failure,
)
from cuprum.pump_events import PumpEvent
from cuprum.pump_observation import _emit_pump_event

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_LOGGER = logging.getLogger("cuprum._pipeline_streams")
_NATIVE_PUMP_FUTURES: set[asyncio.Future[int]] = set()
"""Executor futures retained until their completion callback releases FDs."""

_DEFAULT_NATIVE_PUMP_CLEANUP_GRACE = 0.5


@dc.dataclass(slots=True)
class _RustPumpState:
    """Capture callback-owned duplicates that native pumping must restore."""

    reader_fd: int
    writer_fd: int
    blocking_mode_guard: _BlockingModeGuard
    resume_reader: cabc.Callable[[], None] | None
    was_cancelled: bool = False
    monotonic_clock: cabc.Callable[[], float] = time.monotonic
    cleanup_grace_s: float = _DEFAULT_NATIVE_PUMP_CLEANUP_GRACE
    was_deferred: bool = False


@dc.dataclass(frozen=True, slots=True)
class _NativePumpFds:
    """Duplicated descriptors whose lifetime belongs to native pumping."""

    reader_fd: int
    writer_fd: int


@dc.dataclass(frozen=True, slots=True)
class _RustPumpHandoff:
    """Raw descriptors and caller-facing cleanup policy for one hop."""

    reader_fd: int
    writer_fd: int
    cleanup_grace_s: float


def _restore_rust_pump_state(state: _RustPumpState) -> None:
    """Restore pipe state before returning reader transport control to asyncio."""
    state.blocking_mode_guard.restore()
    _resume_reader_transport(state.resume_reader)


def _close_rust_pump_state_fds(state: _RustPumpState) -> None:
    """Release callback-owned duplicates after restoring their state."""
    _close_rust_state_fd(state.reader_fd)
    _close_rust_state_fd(state.writer_fd)


def _duplicate_native_pump_fds(state: _RustPumpState) -> _NativePumpFds:
    """Duplicate callback-owned descriptors for the native worker."""
    reader_fd = os.dup(state.reader_fd)
    try:
        writer_fd = os.dup(state.writer_fd)
    except BaseException:
        _close_rust_reader_fd(reader_fd)
        raise
    return _NativePumpFds(reader_fd=reader_fd, writer_fd=writer_fd)


def _create_rust_pump_state(
    handoff: _RustPumpHandoff,
    resume_reader: cabc.Callable[[], None] | None,
) -> _RustPumpState:
    """Duplicate a hand-off's descriptor state for completion-owned cleanup."""
    reader_fd = os.dup(handoff.reader_fd)
    writer_fd: int | None = None
    try:
        writer_fd = os.dup(handoff.writer_fd)
        blocking_mode_guard = _BlockingModeGuard.engage(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
        )
    except BaseException:
        _close_rust_reader_fd(reader_fd)
        if writer_fd is not None:
            _close_rust_writer_fd(writer_fd)
        raise
    if writer_fd is None:
        msg = "writer descriptor duplication unexpectedly produced no descriptor"
        raise RuntimeError(msg)
    return _RustPumpState(
        reader_fd=reader_fd,
        writer_fd=writer_fd,
        blocking_mode_guard=blocking_mode_guard,
        resume_reader=resume_reader,
        cleanup_grace_s=handoff.cleanup_grace_s,
    )


def _complete_rust_pump(
    completed: asyncio.Future[int],
    *,
    cleanup_complete: asyncio.Future[None],
    native_fds: _NativePumpFds,
    state: _RustPumpState,
) -> None:
    """Release native-pump resources after its executor worker settles."""
    try:
        if not completed.cancelled():
            error = completed.exception()
            if state.was_cancelled and error is not None:
                _log_rust_pump_failed_after_cancel(error)
    finally:
        _close_rust_reader_fd(native_fds.reader_fd)
        _close_rust_writer_fd(native_fds.writer_fd)
        try:
            with _suppressed_teardown_failure(
                _LOGGER,
                "restore_state",
                OSError,
                ValueError,
            ):
                _restore_rust_pump_state(state)
        finally:
            _close_rust_pump_state_fds(state)
            if state.was_deferred:
                _log_native_pump_cleanup_deferred(_LOGGER)
            if not cleanup_complete.done():
                cleanup_complete.set_result(None)
            _NATIVE_PUMP_FUTURES.discard(completed)


def _defer_native_pump_cleanup(
    *,
    state: _RustPumpState | None,
    monotonic_clock: cabc.Callable[[], float],
    started_at: float,
) -> None:
    """Mark callback-owned cleanup deferred and report its caller-bound expiry."""
    if state is not None:
        state.was_deferred = True
    _log_native_pump_cleanup_grace_expired(
        _LOGGER,
        max(0.0, monotonic_clock() - started_at),
    )


async def _await_native_pump_cleanup(
    cleanup_complete: asyncio.Future[None],
    *,
    monotonic_clock: cabc.Callable[[], float],
    cleanup_grace_s: float,
    state: _RustPumpState | None = None,
) -> None:
    """Wait for cleanup or defer it when its caller grace expires."""
    started_at = monotonic_clock()
    deadline = started_at + cleanup_grace_s
    _log_native_pump_cleanup_started(_LOGGER)
    try:
        while not cleanup_complete.done():
            remaining_s = deadline - monotonic_clock()
            if remaining_s <= 0:
                _defer_native_pump_cleanup(
                    state=state,
                    monotonic_clock=monotonic_clock,
                    started_at=started_at,
                )
                return
            try:
                async with asyncio.timeout(remaining_s):
                    await asyncio.shield(cleanup_complete)
            except asyncio.CancelledError:
                continue
            except TimeoutError:
                if not cleanup_complete.done():
                    _defer_native_pump_cleanup(
                        state=state,
                        monotonic_clock=monotonic_clock,
                        started_at=started_at,
                    )
                    return
    finally:
        if cleanup_complete.done():
            _log_native_pump_cleanup_completed(_LOGGER, monotonic_clock() - started_at)


async def _run_rust_pump_with_blocking_fds(
    *,
    state: _RustPumpState,
) -> None:
    """Run the native pump while its executor future owns cleanup."""
    from cuprum._streams_rs import rust_pump_stream

    # The worker borrows its reader and consumes its writer. Both are separate
    # from the paused asyncio transport, so a deferred caller cannot close or
    # reuse a descriptor native I/O still needs.
    loop = asyncio.get_running_loop()
    cleanup_complete = loop.create_future()
    native_fds: _NativePumpFds | None = None
    try:
        native_fds = _duplicate_native_pump_fds(state)
        native_pump = loop.run_in_executor(
            None,
            rust_pump_stream,
            native_fds.reader_fd,
            native_fds.writer_fd,
        )
    except BaseException:
        if native_fds is not None:
            _close_rust_reader_fd(native_fds.reader_fd)
            _close_rust_writer_fd(native_fds.writer_fd)
        _restore_rust_pump_state(state)
        _close_rust_pump_state_fds(state)
        raise
    if native_fds is None:
        msg = "native descriptor duplication unexpectedly produced no descriptors"
        raise RuntimeError(msg)
    _NATIVE_PUMP_FUTURES.add(native_pump)
    native_pump.add_done_callback(
        functools.partial(
            _complete_rust_pump,
            cleanup_complete=cleanup_complete,
            native_fds=native_fds,
            state=state,
        )
    )
    try:
        await asyncio.shield(native_pump)
    except asyncio.CancelledError:
        state.was_cancelled = True
        await _await_native_pump_cleanup(
            cleanup_complete,
            monotonic_clock=state.monotonic_clock,
            cleanup_grace_s=state.cleanup_grace_s,
            state=state,
        )
        raise
    except BaseException:
        await asyncio.shield(cleanup_complete)
        raise
    await asyncio.shield(cleanup_complete)


def _log_rust_pump_failed_after_cancel(error: BaseException) -> None:
    """Record a native-pump failure masked by caller-requested cancellation."""
    _LOGGER.debug(
        "Rust pump failed while its hop was being cancelled",
        exc_info=error,
        extra={"cuprum_action": "rust_pump_failed_after_cancel"},
    )
    _emit_pump_event(PumpEvent(phase="failed_after_cancel"))
