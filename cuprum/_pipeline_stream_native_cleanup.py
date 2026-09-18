"""Own native-pump executor cleanup, spans, and deferred descriptor hand-back.

This module owns callback-managed native-pump descriptors and their
executor-hop spans. Rust owns a submitted writer duplicate; the completion
callback closes its borrowed reader and callback duplicates, ends the hop
spans with the worker's terminal outcome, restores blocking mode, and resumes
the paused reader only after native I/O has settled.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import logging
import os
import typing as typ

from cuprum import _pipeline_rust_pump_completion as _pump_completion
from cuprum import _pipeline_stream_cleanup_observation as _pump_obs
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
    _log_native_pump_cleanup,
    _log_native_pump_handoff_failed,
)
from cuprum._pipeline_stream_fds import (
    _BlockingModeGuard,
    _close_rust_reader_fd,
    _close_rust_state_fd,
    _close_rust_writer_fd,
    _open_native_pump_worker_fds,
    _resume_reader_transport,
    _suppressed_teardown_failure,
)
from cuprum.pump_events import RustPumpHandoffOutcome
from cuprum.pump_observation import _emit_rust_pump_handoff_outcome
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


def _create_rust_pump_state(
    handoff: _RustPumpHandoff,
    resume_reader: cabc.Callable[[], None] | None,
    release_reader: cabc.Callable[[], None] | None = None,
) -> _RustPumpState:
    """Duplicate a hand-off's descriptor state for completion-owned cleanup."""
    reader_fd, writer_fd = _duplicate_rust_pump_state_fds(handoff)
    blocking_mode_guard = _engage_rust_pump_blocking_mode(reader_fd, writer_fd)
    return _RustPumpState(
        reader_fd=reader_fd,
        writer_fd=writer_fd,
        blocking_mode_guard=blocking_mode_guard,
        resume_reader=resume_reader,
        release_reader=release_reader,
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


def _finalize_native_pump_resources(
    completed: cf.Future[int],
    cleanup: _RustPumpCompletion,
) -> None:
    """Release callback-owned descriptors after shared terminal handling."""
    try:
        _close_rust_reader_fd(cleanup.native_fds.reader_fd)
    finally:
        try:
            with _suppressed_teardown_failure(
                _LOGGER,
                "restore_state",
                OSError,
                ValueError,
            ):
                cleanup.state.blocking_mode_guard.restore()
        finally:
            try:
                _close_rust_pump_state_fds(cleanup.state)
            finally:
                if cleanup.state.complete_cleanup():
                    _log_native_pump_cleanup(_LOGGER, "cleanup_deferred")
                cleanup.runtime.retained_futures.discard(completed)


def _schedule_native_pump_completion(cleanup: _RustPumpCompletion) -> None:
    """Schedule reader resumption on the original loop when it still exists."""
    loop = cleanup.cleanup_complete.get_loop()
    # The loop may close after the caller receives cancellation. Descriptor
    # finalization above remains valid; a closed transport cannot resume.
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(
            functools.partial(_resume_reader_after_rust_pump_cleanup, cleanup)
        )


def _finalize_rust_pump_cleanup(
    completed: cf.Future[int],
    cleanup: _RustPumpCompletion,
) -> None:
    """Close worker descriptors and schedule reader resumption when possible."""
    _pump_completion._complete_rust_pump(
        completed,
        state=cleanup.state,
        hooks=_pump_completion._RustPumpCompletionHooks(
            close_spans=lambda outcome, total_bytes: _close_pump_hop_spans(
                cleanup.pump_hop_spans,
                outcome=outcome,
                total_bytes=total_bytes,
            ),
            restore_state=functools.partial(
                _finalize_native_pump_resources,
                completed,
                cleanup,
            ),
            signal_completion=functools.partial(
                _schedule_native_pump_completion,
                cleanup,
            ),
        ),
        logger=_LOGGER,
    )


async def _await_native_pump_cleanup(
    cleanup_complete: asyncio.Future[None],
    *,
    monotonic_clock: cabc.Callable[[], float],
    cleanup_grace_s: float,
    state: _RustPumpState | None = None,
) -> None:
    """Wait for cleanup or defer it when its caller grace expires."""
    await _pump_obs._await_native_pump_cleanup(
        cleanup_complete,
        wait=_pump_obs._NativePumpCleanupWait(
            _LOGGER, monotonic_clock, cleanup_grace_s, state
        ),
    )


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


def _consume_native_pump_result(future: asyncio.Future[int]) -> None:
    """Retrieve a worker failure after caller cancellation leaves it unawaited."""
    if not future.cancelled():
        future.exception()
