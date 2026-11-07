"""Finalize a submitted native pump after its executor future settles.

The hand-off module owns acquisition, blocking activation, and submission.
Once native work is running, the executor future owns the descriptors, so this
module closes the callback's borrowed reader and duplicates, restores blocking
mode, resumes the paused reader, and settles the completion future. That split
keeps the hand-off ordering independent from the cleanup paths a cancelled or
failed worker can take.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import typing as typ

from cuprum import _pipeline_rust_pump_completion as _pump_completion
from cuprum import _pipeline_stream_cleanup_observation as _pump_obs
from cuprum._pipeline_stream_cleanup_observation import _log_native_pump_cleanup
from cuprum._pipeline_stream_fds import (
    _close_rust_reader_fd,
    _close_rust_state_fd,
    _resume_reader_transport,
    _suppressed_teardown_failure,
)
from cuprum.pump_span_observation import _close_pump_hop_spans

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc
    import concurrent.futures as cf

    from cuprum._pipeline_native_pump_types import _RustPumpCompletion, _RustPumpState

_LOGGER = logging.getLogger("cuprum._pipeline_streams")


def _restore_rust_pump_state(state: _RustPumpState) -> None:
    """Restore pipe state before returning reader transport control to asyncio."""
    state.blocking_mode_guard.restore()
    _resume_reader_transport(state.resume_reader)


def _close_rust_pump_state_fds(state: _RustPumpState) -> None:
    """Release callback-owned duplicates after restoring their state."""
    _close_rust_state_fd(state.reader_fd)
    _close_rust_state_fd(state.writer_fd)


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


def _consume_native_pump_result(future: asyncio.Future[int]) -> None:
    """Retrieve a worker failure after caller cancellation leaves it unawaited."""
    if not future.cancelled():
        future.exception()
