"""Own native-pump executor cleanup and deferred descriptor hand-back.

This module owns callback-managed native-pump descriptors. Rust owns a
submitted writer duplicate; the completion callback closes its borrowed reader
and callback duplicates, restores blocking mode, and resumes the paused reader
only after native I/O has settled.
"""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import contextlib
import contextvars
import dataclasses as dc
import functools
import logging
import os
import time
import typing as typ

from cuprum._pipeline_stream_cleanup_observation import (
    _log_native_pump_cleanup,
    _log_native_pump_handoff_failed,
)
from cuprum._pipeline_stream_fds import (
    _BlockingModeGuard,
    _close_rust_reader_fd,
    _close_rust_state_fd,
    _close_rust_writer_fd,
    _resume_reader_transport,
    _suppressed_teardown_failure,
)
from cuprum.pump_events import PumpEvent, RustPumpHandoffOutcome
from cuprum.pump_observation import _emit_pump_event, _emit_rust_pump_handoff_outcome

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_LOGGER = logging.getLogger("cuprum._pipeline_streams")
_NATIVE_PUMP_FUTURES: set[cf.Future[int]] = set()
"""Executor futures retained until their completion callback releases FDs."""

_NATIVE_PUMP_EXECUTOR = cf.ThreadPoolExecutor(thread_name_prefix="cuprum-native-pump")
"""Executor kept outside ``asyncio.run`` shutdown for uninterruptible native I/O."""

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
class _RustPumpCompletion:
    """Retain callback-owned resources until native pumping has settled."""

    cleanup_complete: asyncio.Future[None]
    native_fds: _NativePumpFds
    state: _RustPumpState
    completion_context: contextvars.Context


@dc.dataclass(frozen=True, slots=True)
class _RustPumpHandoff:
    """Raw descriptors and caller-facing cleanup policy for one hop."""

    reader_fd: int
    writer_fd: int
    cleanup_grace_s: float


class _RustPumpStateDuplicationError(Exception):
    """Retain a state-duplication failure for the caller to re-raise."""

    def __init__(self, error: OSError | ValueError) -> None:
        """Store the original descriptor duplication failure."""
        super().__init__(str(error))
        self.error = error


class _RustPumpBlockingModeError(Exception):
    """Retain a blocking-mode refusal for the caller to turn into fallback."""

    def __init__(self, error: OSError | ValueError) -> None:
        """Store the original blocking-mode refusal."""
        super().__init__(str(error))
        self.error = error


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


def _duplicate_rust_pump_state_fds(
    handoff: _RustPumpHandoff,
) -> tuple[int, int]:
    """Duplicate the descriptors whose state the completion callback owns."""
    try:
        reader_fd = os.dup(handoff.reader_fd)
    except (OSError, ValueError) as error:
        raise _RustPumpStateDuplicationError(error) from error
    try:
        writer_fd = os.dup(handoff.writer_fd)
    except (OSError, ValueError) as error:
        _close_rust_reader_fd(reader_fd)
        raise _RustPumpStateDuplicationError(error) from error
    except BaseException:
        _close_rust_reader_fd(reader_fd)
        raise
    return reader_fd, writer_fd


def _engage_rust_pump_blocking_mode(
    reader_fd: int,
    writer_fd: int,
) -> _BlockingModeGuard:
    """Engage blocking mode, rolling back both state duplicates on failure."""
    try:
        return _BlockingModeGuard.engage(reader_fd=reader_fd, writer_fd=writer_fd)
    except (OSError, ValueError) as error:
        _close_rust_reader_fd(reader_fd)
        _close_rust_writer_fd(writer_fd)
        raise _RustPumpBlockingModeError(error) from error
    except BaseException:
        _close_rust_reader_fd(reader_fd)
        _close_rust_writer_fd(writer_fd)
        raise


def _create_rust_pump_state(
    handoff: _RustPumpHandoff,
    resume_reader: cabc.Callable[[], None] | None,
) -> _RustPumpState:
    """Duplicate a hand-off's descriptor state for completion-owned cleanup."""
    reader_fd, writer_fd = _duplicate_rust_pump_state_fds(handoff)
    blocking_mode_guard = _engage_rust_pump_blocking_mode(reader_fd, writer_fd)
    return _RustPumpState(
        reader_fd=reader_fd,
        writer_fd=writer_fd,
        blocking_mode_guard=blocking_mode_guard,
        resume_reader=resume_reader,
        cleanup_grace_s=handoff.cleanup_grace_s,
    )


def _resume_reader_after_rust_pump_cleanup(cleanup: _RustPumpCompletion) -> None:
    """Resume asyncio's reader and settle normal loop-owned cleanup waiting."""
    try:
        with _suppressed_teardown_failure(
            _LOGGER,
            "resume_reader",
            OSError,
            ValueError,
        ):
            _resume_reader_transport(cleanup.state.resume_reader)
    finally:
        if not cleanup.cleanup_complete.done():
            cleanup.cleanup_complete.set_result(None)


def _complete_rust_pump(
    completed: cf.Future[int],
    cleanup: _RustPumpCompletion,
) -> None:
    """Finalize descriptors independently of whether the original loop survives."""
    cleanup.completion_context.run(
        _finalize_rust_pump_cleanup,
        completed,
        cleanup,
    )


def _finalize_rust_pump_cleanup(
    completed: cf.Future[int],
    cleanup: _RustPumpCompletion,
) -> None:
    """Close worker descriptors and schedule reader resumption when possible."""
    try:
        if not completed.cancelled():
            error = completed.exception()
            if cleanup.state.was_cancelled and error is not None:
                _log_rust_pump_failed_after_cancel(error)
    finally:
        _close_rust_reader_fd(cleanup.native_fds.reader_fd)
        try:
            with _suppressed_teardown_failure(
                _LOGGER,
                "restore_state",
                OSError,
                ValueError,
            ):
                cleanup.state.blocking_mode_guard.restore()
        finally:
            _close_rust_pump_state_fds(cleanup.state)
            if cleanup.state.was_deferred:
                _log_native_pump_cleanup(_LOGGER, "cleanup_deferred")
            _NATIVE_PUMP_FUTURES.discard(completed)
    loop = cleanup.cleanup_complete.get_loop()
    # The loop may close after the caller receives cancellation. Descriptor
    # finalization above remains valid; a closed transport cannot resume.
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(
            functools.partial(_resume_reader_after_rust_pump_cleanup, cleanup)
        )


def _defer_native_pump_cleanup(
    *,
    state: _RustPumpState | None,
    monotonic_clock: cabc.Callable[[], float],
    started_at: float,
) -> None:
    """Mark callback-owned cleanup deferred and report its caller-bound expiry."""
    if state is not None:
        state.was_deferred = True
    _log_native_pump_cleanup(
        _LOGGER,
        "cleanup_grace_expired",
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
    _log_native_pump_cleanup(_LOGGER, "cleanup_started")
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
            _log_native_pump_cleanup(
                _LOGGER,
                "cleanup_completed",
                monotonic_clock() - started_at,
            )


def _start_rust_pump_with_cleanup(
    state: _RustPumpState,
) -> tuple[cf.Future[int], asyncio.Future[None]]:
    """Start the native pump and register its completion-owned cleanup."""
    from cuprum._streams_rs import rust_pump_stream

    # The worker borrows its reader and consumes its writer. Both are separate
    # from the paused asyncio transport, so a deferred caller cannot close or
    # reuse a descriptor native I/O still needs.
    loop = asyncio.get_running_loop()
    cleanup_complete = typ.cast("asyncio.Future[None]", loop.create_future())
    try:
        native_fds = _duplicate_native_pump_fds(state)
    except BaseException as error:
        _log_native_pump_handoff_failed(_LOGGER, "duplicate_writer", error)
        _restore_rust_pump_state(state)
        _close_rust_pump_state_fds(state)
        _emit_rust_pump_handoff_outcome(RustPumpHandoffOutcome.DUPLICATE_WRITER_FAILED)
        raise
    try:
        worker_context = contextvars.copy_context()
        completion_context = contextvars.copy_context()
        native_pump = typ.cast(
            "cf.Future[int]",
            _NATIVE_PUMP_EXECUTOR.submit(
                worker_context.run,
                rust_pump_stream,
                native_fds.reader_fd,
                native_fds.writer_fd,
            ),
        )
    except BaseException as error:
        _log_native_pump_handoff_failed(_LOGGER, "executor_submission", error)
        _close_rust_reader_fd(native_fds.reader_fd)
        _close_rust_writer_fd(native_fds.writer_fd)
        _restore_rust_pump_state(state)
        _close_rust_pump_state_fds(state)
        _emit_rust_pump_handoff_outcome(
            RustPumpHandoffOutcome.EXECUTOR_SUBMISSION_REJECTED
        )
        raise
    _NATIVE_PUMP_FUTURES.add(native_pump)
    cleanup = _RustPumpCompletion(
        cleanup_complete=cleanup_complete,
        native_fds=native_fds,
        state=state,
        completion_context=completion_context,
    )
    native_pump.add_done_callback(
        functools.partial(_complete_rust_pump, cleanup=cleanup)
    )
    _emit_rust_pump_handoff_outcome(RustPumpHandoffOutcome.SUBMITTED)
    return native_pump, cleanup_complete


async def _run_rust_pump_with_blocking_fds(
    *,
    state: _RustPumpState,
) -> None:
    """Run the native pump while its executor future owns cleanup."""
    native_pump, cleanup_complete = _start_rust_pump_with_cleanup(state)
    awaited_native_pump = asyncio.wrap_future(native_pump)
    awaited_native_pump.add_done_callback(_consume_native_pump_result)
    try:
        await asyncio.shield(awaited_native_pump)
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


def _consume_native_pump_result(future: asyncio.Future[int]) -> None:
    """Retrieve a worker failure after caller cancellation leaves it unawaited."""
    if not future.cancelled():
        future.exception()


def _log_rust_pump_failed_after_cancel(error: BaseException) -> None:
    """Record a native-pump failure masked by caller-requested cancellation."""
    _LOGGER.debug(
        "Rust pump failed while its hop was being cancelled",
        exc_info=error,
        extra={"cuprum_action": "rust_pump_failed_after_cancel"},
    )
    _emit_pump_event(PumpEvent(phase="failed_after_cancel"))
