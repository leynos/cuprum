"""Supply the acquire, drain, activate, and submit steps of the hand-off.

The ordering — pause, acquire before the first await, drain, activate, submit —
belongs to `_pipeline_streams._pump_over_raw_fds`; this module supplies the
steps and keeps each failure shape local: acquisition returns `None` to allow
the Python fallback, activation declines with `_RustPumpBlockingModeError`, and
`_close_unactivated_rust_pump_fds` releases a pair activation never took over.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import logging
import os
import typing as typ

from cuprum._pipeline_native_pump_runtime import (
    _DEFAULT_NATIVE_PUMP_RUNTIME,
    _NativePumpRuntime,
)
from cuprum._pipeline_native_pump_types import (
    _NativePumpFds,
    _RustPumpBlockingModeError,
    _RustPumpCompletion,
    _RustPumpHandoff,
    _RustPumpState,
    _RustPumpStateDuplicationError,
)
from cuprum._pipeline_stream_cleanup_observation import (
    _log_native_pump_handoff_failed,
)
from cuprum._pipeline_stream_fds import (
    _BlockingModeGuard,
    _close_native_pump_worker_fd,
    _close_rust_reader_fd,
    _close_rust_writer_fd,
    _NativePumpWorkerFds,
    _open_native_pump_worker_fds,
    _ReaderPause,
    _resume_reader_transport,
)
from cuprum._pipeline_stream_native_completion import (
    _await_native_pump_cleanup,
    _close_rust_pump_state_fds,
    _complete_rust_pump,
    _consume_native_pump_result,
    _restore_rust_pump_state,
)
from cuprum.pump_events import RustPumpHandoffOutcome
from cuprum.pump_observation import (
    _emit_rust_pump_handoff_outcome,
    _emit_rust_pump_hold,
    _release_declined_hold,
    _settle_hold_outcome,
)
from cuprum.pump_span_events import (
    NATIVE_PUMP_BUFFER_SIZE,
    PUMP_HOP_BUFFER_SIZE_ATTRIBUTE,
    PUMP_HOP_OPERATION_ATTRIBUTE,
    PumpHopOutcome,
)
from cuprum.pump_span_observation import (
    _EMPTY_PUMP_HOP_SPANS,
    _close_pump_hop_spans,
    _open_pump_hop_spans,
    current_pump_span_tracers,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import concurrent.futures as cf

    from cuprum.pump_span_observation import _PumpHopSpans

_LOGGER = logging.getLogger("cuprum._pipeline_streams")


def _duplicate_native_pump_fds(state: _RustPumpState) -> _NativePumpFds:
    """Duplicate callback-owned descriptors for the native worker."""
    reader_fd = os.dup(state.reader_fd)
    try:
        writer_fd = os.dup(state.writer_fd)
    except BaseException:
        _close_rust_reader_fd(reader_fd)
        raise
    return _NativePumpFds(reader_fd=reader_fd, writer_fd=writer_fd)


def _close_native_pump_fds(native_fds: _NativePumpFds | None) -> None:
    """Release the native descriptors when executor submission never began."""
    if native_fds is None:
        return
    try:
        _close_rust_reader_fd(native_fds.reader_fd)
    finally:
        _close_rust_writer_fd(native_fds.writer_fd)


def _rollback_rust_pump_start(
    state: _RustPumpState,
    outcome: RustPumpHandoffOutcome,
    *,
    native_fds: _NativePumpFds | None = None,
    pump_hop_spans: _PumpHopSpans = _EMPTY_PUMP_HOP_SPANS,
) -> None:
    """Restore state and publish failure after a hand-off setup error."""
    with contextlib.ExitStack() as rollback:
        rollback.callback(_emit_rust_pump_handoff_outcome, outcome)
        rollback.callback(_close_rust_pump_state_fds, state)
        rollback.callback(_restore_rust_pump_state, state)
        rollback.callback(_close_native_pump_fds, native_fds)
        _close_pump_hop_spans(
            pump_hop_spans,
            outcome=PumpHopOutcome.FAILED,
            total_bytes=None,
        )


def _release_activated_rust_pump_hold(state: _RustPumpState) -> None:
    """Release the coordinator's hold on an activated but unsubmitted pair.

    The hold precedes the completion callback's ownership, so a shutdown signal
    from a hold observer would otherwise strand the open pair, its blocking
    modes, and the paused reader; rolling back first keeps the signal going.
    """
    try:
        _emit_rust_pump_hold(-1)
    except BaseException:
        _rollback_rust_pump_start(
            state,
            RustPumpHandoffOutcome.SUBMISSION_ABANDONED,
        )
        raise


def _open_rust_pump_hop_spans() -> _PumpHopSpans:
    """Open executor-hop spans only when a tracer is observing this task."""
    if not current_pump_span_tracers():
        return _EMPTY_PUMP_HOP_SPANS
    return _open_pump_hop_spans({
        PUMP_HOP_OPERATION_ATTRIBUTE: "rust_pump",
        PUMP_HOP_BUFFER_SIZE_ATTRIBUTE: NATIVE_PUMP_BUFFER_SIZE,
    })


def _submit_rust_pump(
    runtime: _NativePumpRuntime,
    rust_pump_stream: cabc.Callable[[int, int], int],
    native_fds: _NativePumpFds,
) -> tuple[cf.Future[int], contextvars.Context]:
    """Submit native pumping and retain the callback's trace context."""
    worker_context = contextvars.copy_context()
    completion_context = contextvars.copy_context()
    native_pump = runtime.executor.submit(
        functools.partial(worker_context.run, rust_pump_stream),
        native_fds.reader_fd,
        native_fds.writer_fd,
    )
    return native_pump, completion_context


def _duplicate_rust_pump_state_fds(
    handoff: _RustPumpHandoff,
) -> tuple[int, int]:
    """Open state descriptors independent from asyncio's transport descriptors."""
    worker_fds = _open_native_pump_worker_fds(
        reader_fd=handoff.reader_fd,
        writer_fd=handoff.writer_fd,
    )
    if worker_fds is None:
        error = OSError("could not create native pump worker descriptors")
        raise _RustPumpStateDuplicationError(error) from error
    return worker_fds.reader_fd, worker_fds.writer_fd


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


def _acquire_rust_pump_worker_fds(
    handoff: _RustPumpHandoff,
) -> _NativePumpWorkerFds | None:
    """Open worker descriptors before the hand-off first awaits.

    The hand-off carries descriptor *numbers*. Opening them after the buffered
    drain would let a transport that closed during that await release its
    number, which another concurrent pipeline can then reuse; the worker would
    attach to the wrong pipe. Opening here runs before the first suspension, so
    the numbers still name the pipe the hand-off was authorized to borrow.

    Returns
    -------
    _NativePumpWorkerFds or None
        The owned worker pair, or ``None`` when the descriptors cannot be
        opened and the hop must fall back to the Python pump.
    """
    try:
        reader_fd, writer_fd = _duplicate_rust_pump_state_fds(handoff)
    except _RustPumpStateDuplicationError:
        return None
    return _NativePumpWorkerFds(reader_fd=reader_fd, writer_fd=writer_fd)


def _close_unactivated_rust_pump_fds(worker_fds: _NativePumpWorkerFds) -> None:
    """Close an acquired pair that never reached blocking activation."""
    try:
        _close_native_pump_worker_fd(worker_fds.reader_fd)
    finally:
        _close_native_pump_worker_fd(worker_fds.writer_fd)


def _release_unactivated_pair(
    worker_fds: _NativePumpWorkerFds,
    reader_pause: _ReaderPause,
    outcome: RustPumpHandoffOutcome,
) -> None:
    """Close the unactivated pair, resume the reader, and settle ``outcome``."""
    _close_unactivated_rust_pump_fds(worker_fds)
    _resume_reader_transport(reader_pause.resume)
    _settle_hold_outcome(outcome)


async def _drain_before_rust_pump_submission(
    worker_fds: _NativePumpWorkerFds,
    reader_pause: _ReaderPause,
    drain: cabc.Callable[[], cabc.Awaitable[None]],
) -> None:
    """Drain the buffered prefix while holding the acquired pair.

    A failed or cancelled drain never falls back: the pair closes, the reader
    resumes, and the error travels. ``drain`` is injected to keep its test seam
    with the coordinator owning the ordering.

    Each failure carries its own bounded outcome, because an operator reading
    the hand-off counter has different questions for each. ``DRAIN_CANCELLED``
    is the caller's cancellation reaching the drain, ``DRAIN_FAILED`` is an
    error raised by the drain itself, and ``HOLD_OBSERVER_SHUTDOWN`` is a
    shutdown signal from a hold observer raised while the acquisition was being
    published, before the drain began. Publishing that acquisition is a
    separate guarded step: the observer's signal is not a drain failure, and
    reporting it as one would hide the drain never having started.
    """
    try:
        # Publication can itself raise the shutdown signal this guard handles.
        _emit_rust_pump_hold(1)
    except BaseException:
        _release_unactivated_pair(
            worker_fds,
            reader_pause,
            RustPumpHandoffOutcome.HOLD_OBSERVER_SHUTDOWN,
        )
        raise
    try:
        await drain()
    except asyncio.CancelledError:
        _release_unactivated_pair(
            worker_fds,
            reader_pause,
            RustPumpHandoffOutcome.DRAIN_CANCELLED,
        )
        raise
    except BaseException:
        _release_unactivated_pair(
            worker_fds,
            reader_pause,
            RustPumpHandoffOutcome.DRAIN_FAILED,
        )
        raise


def _activate_rust_pump_state_or_release(
    worker_fds: _NativePumpWorkerFds,
    reader_pause: _ReaderPause,
    handoff: _RustPumpHandoff,
) -> _RustPumpState | None:
    """Activate the pair, or release it and return ``None`` on a decline.

    Activation closes the pair when it refuses, so only the reader and the hold
    remain. A refusal is the one error answered with the Python pump; anything
    else is re-raised after the same release, because a shutdown signal must
    not be reported as a routing decision.

    Returns
    -------
    _RustPumpState or None
        The completion-owned state, or ``None`` when the hop must fall back.
    """
    try:
        return _activate_rust_pump_state(worker_fds, reader_pause, handoff)
    except BaseException as error:
        _resume_reader_transport(reader_pause.resume)
        if not isinstance(error, _RustPumpBlockingModeError):
            _emit_rust_pump_hold(-1)
            raise
        _release_declined_hold()
        return None


def _activate_rust_pump_state(
    worker_fds: _NativePumpWorkerFds,
    reader_pause: _ReaderPause,
    handoff: _RustPumpHandoff,
) -> _RustPumpState:
    """Engage blocking mode on acquired descriptors and build completion state.

    Blocking activation stays after the buffered drain because ``os.dup``
    shares the transport's status flags on non-Linux platforms, so an early
    switch would block asyncio's own descriptor during the drain.
    ``_engage_rust_pump_blocking_mode`` closes both descriptors when it fails.

    Returns
    -------
    _RustPumpState
        Completion-owned state carrying the blocking-mode guard and callbacks.
    """
    blocking_mode_guard = _engage_rust_pump_blocking_mode(
        worker_fds.reader_fd,
        worker_fds.writer_fd,
    )
    try:
        return _RustPumpState(
            reader_fd=worker_fds.reader_fd,
            writer_fd=worker_fds.writer_fd,
            blocking_mode_guard=blocking_mode_guard,
            resume_reader=reader_pause.resume,
            release_reader=reader_pause.release,
            cleanup_grace_s=handoff.cleanup_grace_s,
        )
    except BaseException:
        # No completion callback exists yet, so the guard's modes and the pair
        # are released here before the caller sees the construction failure.
        blocking_mode_guard.restore()
        _close_unactivated_rust_pump_fds(worker_fds)
        raise


def _start_rust_pump_with_cleanup(
    state: _RustPumpState,
    *,
    runtime: _NativePumpRuntime = _DEFAULT_NATIVE_PUMP_RUNTIME,
) -> tuple[cf.Future[int], asyncio.Future[None]]:
    """Start the native pump and register its completion-owned cleanup."""
    try:
        from cuprum._streams_rs import rust_pump_stream
    except BaseException:
        _rollback_rust_pump_start(
            state,
            RustPumpHandoffOutcome.NATIVE_LOAD_FAILED,
        )
        raise
    loop = asyncio.get_running_loop()
    cleanup_complete = typ.cast("asyncio.Future[None]", loop.create_future())
    try:
        native_fds = _duplicate_native_pump_fds(state)
    except BaseException as error:
        _log_native_pump_handoff_failed(_LOGGER, "duplicate_writer", error)
        _rollback_rust_pump_start(
            state,
            RustPumpHandoffOutcome.DUPLICATE_WRITER_FAILED,
        )
        raise
    pump_hop_spans = _EMPTY_PUMP_HOP_SPANS
    try:
        pump_hop_spans = _open_rust_pump_hop_spans()
        native_pump, completion_context = _submit_rust_pump(
            runtime,
            rust_pump_stream,
            native_fds,
        )
    except BaseException as error:
        _log_native_pump_handoff_failed(_LOGGER, "executor_submission", error)
        _rollback_rust_pump_start(
            state,
            RustPumpHandoffOutcome.EXECUTOR_SUBMISSION_REJECTED,
            native_fds=native_fds,
            pump_hop_spans=pump_hop_spans,
        )
        raise
    runtime.retained_futures.add(native_pump)
    cleanup = _RustPumpCompletion(
        cleanup_complete=cleanup_complete,
        native_fds=native_fds,
        state=state,
        runtime=runtime,
        completion_context=completion_context,
        pump_hop_spans=pump_hop_spans,
    )
    native_pump.add_done_callback(
        functools.partial(_complete_rust_pump, cleanup=cleanup)
    )
    _emit_rust_pump_handoff_outcome(RustPumpHandoffOutcome.SUBMITTED)
    return native_pump, cleanup_complete


async def _run_rust_pump_with_blocking_fds(
    *,
    state: _RustPumpState,
    runtime: _NativePumpRuntime = _DEFAULT_NATIVE_PUMP_RUNTIME,
) -> None:
    """Run the native pump while its executor future owns cleanup."""
    native_pump, cleanup_complete = _start_rust_pump_with_cleanup(
        state,
        runtime=runtime,
    )
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
