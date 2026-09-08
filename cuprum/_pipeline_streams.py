"""Pipeline stream pumping, capture collection, and backend dispatch."""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses as dc
import logging
import time
import typing as typ
from functools import partial

from cuprum import _pipeline_stream_cleanup_observation as _pump_obs
from cuprum._backend import StreamBackend, get_stream_backend
from cuprum._pipeline_pipe_tasks import (
    _create_pipe_tasks as _create_pipe_tasks_with_context,
)
from cuprum._pipeline_stream_fds import (
    _BlockingModeGuard,
    _close_native_pump_worker_fd,
    _close_native_pump_worker_fds,
    _close_rust_writer_fd,
    _extract_stream_fd,
    _open_native_pump_worker_fds,
    _pause_reader_transport,
    _resume_reader_transport,
    _suppressed_teardown_failure,
)
from cuprum._streams import _close_stream_writer, _pump_stream
from cuprum._streams_pump import _current_read_size
from cuprum.pump_events import RustPumpDeclineReason, RustPumpHandoffOutcome
from cuprum.pump_observation import _emit_rust_pump_handoff_outcome

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import _StageObservation


_LOGGER = logging.getLogger(__name__)
_log_rust_pump_declined = partial(_pump_obs._log_native_pump_declined, _LOGGER)


@dc.dataclass(slots=True)
class _PumpStreamDispatchTestHooks:
    """Test-only overrides for stream dispatch."""

    force_fd_extraction_failure: bool = False
    on_rust_fd_path_attempt: cabc.Callable[[], None] | None = None
    raw_fd_extractor: (
        cabc.Callable[[asyncio.StreamReader | asyncio.StreamWriter | None], int | None]
        | None
    ) = None
    python_pump: (
        cabc.Callable[
            [asyncio.StreamReader | None, asyncio.StreamWriter | None],
            cabc.Awaitable[None],
        ]
        | None
    ) = None


@dc.dataclass(slots=True)
class _RustPumpState:
    """Capture transport-owned state that native pumping must restore."""

    reader_fd: int
    writer_fd: int
    blocking_mode_guard: _BlockingModeGuard
    resume_reader: cabc.Callable[[], None] | None
    close_reader_fd: bool = False
    close_writer_fd: bool = False
    was_cancelled: bool = False
    monotonic_clock: cabc.Callable[[], float] = time.monotonic


_PUMP_STREAM_DISPATCH_TEST_HOOKS = _PumpStreamDispatchTestHooks()


def configure_pump_stream_dispatch_for_testing(
    *,
    force_fd_extraction_failure: bool = False,
    on_rust_fd_path_attempt: cabc.Callable[[], None] | None = None,
    raw_fd_extractor: (
        cabc.Callable[[asyncio.StreamReader | asyncio.StreamWriter | None], int | None]
        | None
    ) = None,
    python_pump: (
        cabc.Callable[
            [asyncio.StreamReader | None, asyncio.StreamWriter | None],
            cabc.Awaitable[None],
        ]
        | None
    ) = None,
) -> None:
    """Configure explicit test hooks for ``_pump_stream_dispatch``."""
    _PUMP_STREAM_DISPATCH_TEST_HOOKS.force_fd_extraction_failure = (
        force_fd_extraction_failure
    )
    _PUMP_STREAM_DISPATCH_TEST_HOOKS.on_rust_fd_path_attempt = on_rust_fd_path_attempt
    _PUMP_STREAM_DISPATCH_TEST_HOOKS.raw_fd_extractor = raw_fd_extractor
    _PUMP_STREAM_DISPATCH_TEST_HOOKS.python_pump = python_pump


def reset_pump_stream_dispatch_for_testing() -> None:
    """Reset ``_pump_stream_dispatch`` test hooks to defaults."""
    _PUMP_STREAM_DISPATCH_TEST_HOOKS.force_fd_extraction_failure = False
    _PUMP_STREAM_DISPATCH_TEST_HOOKS.on_rust_fd_path_attempt = None
    _PUMP_STREAM_DISPATCH_TEST_HOOKS.raw_fd_extractor = None
    _PUMP_STREAM_DISPATCH_TEST_HOOKS.python_pump = None


def _restore_rust_pump_state(state: _RustPumpState) -> None:
    """Restore pipe state before returning reader transport control to asyncio."""
    try:
        _close_native_worker_fds(state)
    finally:
        try:
            state.blocking_mode_guard.restore()
        finally:
            _resume_reader_transport(state.resume_reader)


def _close_native_worker_fds(state: _RustPumpState) -> None:
    """Close worker descriptors that have not crossed Rust's ownership boundary."""
    if state.close_reader_fd:
        _close_native_pump_worker_fd(state.reader_fd)
        state.close_reader_fd = False
    if state.close_writer_fd:
        _close_rust_writer_fd(state.writer_fd)
        state.close_writer_fd = False


def _complete_rust_pump(
    completed: asyncio.Future[int],
    *,
    cleanup_complete: asyncio.Future[None],
    state: _RustPumpState,
) -> None:
    """Release native-pump resources after its executor worker settles."""
    try:
        if not completed.cancelled():
            error = completed.exception()
            if state.was_cancelled and error is not None:
                _pump_obs._log_native_pump_failed_after_cancel(_LOGGER, error)
    finally:
        try:
            with _suppressed_teardown_failure(
                _LOGGER,
                "restore_state",
                OSError,
                ValueError,
            ):
                _restore_rust_pump_state(state)
        finally:
            if not cleanup_complete.done():
                cleanup_complete.set_result(None)


async def _await_native_pump_cleanup(
    cleanup_complete: asyncio.Future[None],
    *,
    monotonic_clock: cabc.Callable[[], float],
) -> None:
    """Wait for native cleanup despite repeated cancellation requests."""
    started_at = monotonic_clock()
    _pump_obs._log_native_pump_cleanup_started(_LOGGER)
    try:
        while not cleanup_complete.done():
            try:
                await asyncio.shield(cleanup_complete)
            except asyncio.CancelledError:
                continue
    finally:
        if cleanup_complete.done():
            duration_s = monotonic_clock() - started_at
            _pump_obs._log_native_pump_cleanup_completed(_LOGGER, duration_s)


async def _run_rust_pump_with_blocking_fds(
    *,
    state: _RustPumpState,
) -> bool:
    """Run the native pump or decline after duplicate setup rollback."""
    loop = asyncio.get_running_loop()
    cleanup_complete = loop.create_future()
    native_pump = _submit_rust_pump(loop=loop, state=state)
    if native_pump is None:
        return False
    native_pump.add_done_callback(
        partial(
            _complete_rust_pump,
            cleanup_complete=cleanup_complete,
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
        )
        raise
    except BaseException:
        await asyncio.shield(cleanup_complete)
        raise
    await asyncio.shield(cleanup_complete)
    return True


def _submit_rust_pump(
    *,
    loop: asyncio.AbstractEventLoop,
    state: _RustPumpState,
) -> asyncio.Future[int] | None:
    """Prepare and submit native work, transferring writer ownership on success."""
    from cuprum._streams_rs import rust_pump_stream

    try:
        context = contextvars.copy_context()
        native_pump = loop.run_in_executor(
            None,
            context.run,
            rust_pump_stream,
            state.reader_fd,
            state.writer_fd,
        )
    except BaseException as error:
        _pump_obs._log_native_pump_handoff_failed(_LOGGER, "executor_submission", error)
        _restore_rust_pump_state(state)
        _emit_rust_pump_handoff_outcome(
            RustPumpHandoffOutcome.EXECUTOR_SUBMISSION_REJECTED
        )
        raise
    state.close_writer_fd = False
    _emit_rust_pump_handoff_outcome(RustPumpHandoffOutcome.SUBMITTED)
    return native_pump


async def _run_rust_pump(
    *,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
    reader_fd: int,
    writer_fd: int,
) -> bool:
    """Run the Rust pump while the executor future owns native cleanup."""
    handled = await _pump_over_raw_fds(
        reader=reader,
        writer=writer,
        reader_fd=reader_fd,
        writer_fd=writer_fd,
    )
    if not handled:
        return False
    # Close asyncio's original transport FD while suppressing a broken pipe.
    with _suppressed_teardown_failure(_LOGGER, "writer_close", OSError):
        await _close_stream_writer(writer)
    return True


async def _pump_over_raw_fds(
    *,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
    reader_fd: int,
    writer_fd: int,
) -> bool:
    """Transfer a hop after acquiring the reader and descriptor hand-off."""
    reader_pause = _pause_reader_transport(reader)
    if not reader_pause.may_hand_off:
        _log_rust_pump_declined(
            reader_pause.decline_reason or RustPumpDeclineReason.READER_PAUSE_FAILED,
        )
        return False
    try:
        # The paused transport cannot refill while buffered bytes transfer.
        await _drain_reader_buffer(reader, writer)
    except BaseException:
        _resume_reader_transport(reader_pause.resume)
        raise
    worker_fds = _open_native_pump_worker_fds(
        reader_fd=reader_fd,
        writer_fd=writer_fd,
    )
    if worker_fds is None:
        _resume_reader_transport(reader_pause.resume)
        _log_rust_pump_declined(RustPumpDeclineReason.RAW_FD_UNAVAILABLE)
        return False
    try:
        blocking_mode_guard = _BlockingModeGuard.engage(
            reader_fd=worker_fds.reader_fd,
            writer_fd=worker_fds.writer_fd,
        )
    except (OSError, ValueError):
        _close_native_pump_worker_fds(worker_fds)
        _resume_reader_transport(reader_pause.resume)
        _log_rust_pump_declined(RustPumpDeclineReason.BLOCKING_MODE_UNAVAILABLE)
        return False
    state = _RustPumpState(
        reader_fd=worker_fds.reader_fd,
        writer_fd=worker_fds.writer_fd,
        blocking_mode_guard=blocking_mode_guard,
        resume_reader=reader_pause.resume,
        close_reader_fd=True,
        close_writer_fd=True,
    )
    return await _run_rust_pump_with_blocking_fds(state=state)


async def _drain_reader_buffer(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
) -> None:
    """Flush bytes already buffered in *reader* to *writer*."""
    # ``getattr`` degrades safely when an implementation lacks this CPython buffer.
    buffered: bytearray | None = getattr(reader, "_buffer", None)
    if not buffered:
        return
    if writer is not None:
        try:
            writer.write(bytes(buffered))
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
    # Either delivery or a closed downstream is now known; a different error
    # propagates with the buffer intact for its caller to handle.
    buffered.clear()


async def _run_python_pump(
    reader: asyncio.StreamReader | None,
    writer: asyncio.StreamWriter | None,
    *,
    read_size: int,
) -> None:
    """Run the configured Python pump implementation."""
    python_pump = _PUMP_STREAM_DISPATCH_TEST_HOOKS.python_pump
    if python_pump is not None:
        await python_pump(reader, writer)
        return
    await _pump_stream(reader, writer, read_size=read_size)


async def _try_rust_pump(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
) -> bool:
    """Attempt to route the pipe hop through the Rust pump."""
    rust_fd_attempt_hook = _PUMP_STREAM_DISPATCH_TEST_HOOKS.on_rust_fd_path_attempt
    if rust_fd_attempt_hook is not None:
        rust_fd_attempt_hook()
    if _PUMP_STREAM_DISPATCH_TEST_HOOKS.force_fd_extraction_failure:
        return False
    extract_raw_fd = _PUMP_STREAM_DISPATCH_TEST_HOOKS.raw_fd_extractor
    extractor = _extract_stream_fd if extract_raw_fd is None else extract_raw_fd
    reader_fd = extractor(reader)
    writer_fd = extractor(writer)
    if reader_fd is None or writer_fd is None:
        _log_rust_pump_declined(RustPumpDeclineReason.RAW_FD_UNAVAILABLE)
        return False
    return await _run_rust_pump(
        reader=reader,
        writer=writer,
        reader_fd=reader_fd,
        writer_fd=writer_fd,
    )


async def _pump_stream_dispatch(
    reader: asyncio.StreamReader | None,
    writer: asyncio.StreamWriter | None,
    *,
    read_size: int | None = None,
) -> None:
    """Route inter-stage pump to the Rust or Python implementation."""
    active_read_size = _current_read_size() if read_size is None else read_size
    if reader is None:
        await _run_python_pump(reader, writer, read_size=active_read_size)
        return

    backend = get_stream_backend()
    if backend is StreamBackend.RUST and await _try_rust_pump(reader, writer):
        return

    await _run_python_pump(reader, writer, read_size=active_read_size)


def _create_pipe_tasks(
    processes: list[asyncio.subprocess.Process],
    observations: tuple[_StageObservation, ...] = (),
) -> list[asyncio.Task[None]]:
    """Create streaming tasks between adjacent pipeline stages."""
    return _create_pipe_tasks_with_context(
        processes,
        observations,
        partial(_pump_stream_dispatch, read_size=_current_read_size()),
    )
