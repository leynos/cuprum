"""Pipeline stream pumping, capture collection, and backend dispatch.

This module handles data movement after ``cuprum._process_lifecycle`` has
spawned each subprocess with the canonical stdio handles from
``cuprum._pipeline_stage_streams``. It creates the tasks that capture final
stdout and per-stage stderr, pumps stdout from one stage into the next stage's
stdin, and chooses between the Python and Rust stream backends for that pump.

The module intentionally consumes the canonical stage stream policy instead of
recomputing it. That keeps lifecycle code responsible for process ownership,
``_pipeline_stage_streams`` responsible for stdio shape, and this module
responsible for moving and collecting bytes once those streams exist.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import logging
import typing as typ

from cuprum._backend import StreamBackend, get_stream_backend
from cuprum._pipeline_config import (
    _PipelineRunConfig as _PipelineRunConfig,
)
from cuprum._pipeline_config import (
    _prepare_pipeline_config as _prepare_pipeline_config,
)
from cuprum._pipeline_stage_streams import (
    _create_stage_capture_tasks as _create_stage_capture_tasks,
)
from cuprum._pipeline_stream_fds import (
    _BlockingModeGuard,
    _extract_stream_fd,
    _paused_reader,
    _suppressed_teardown_failure,
)
from cuprum._streams import _close_stream_writer, _pump_stream
from cuprum.pump_events import PumpEvent, RustPumpDeclineReason
from cuprum.pump_observation import _emit_pump_event

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_LOGGER = logging.getLogger(__name__)

# Retained under its historical private name so existing internal call sites and
# tests keep working now that the closed set is published beside the event type
# that carries it.
_RustPumpDeclineReason = RustPumpDeclineReason


def _log_rust_pump_declined(reason: RustPumpDeclineReason) -> None:
    """Record why an inter-stage hop fell back from the Rust pump to Python.

    Each decline is a silent performance decision: the hop still completes
    correctly on the Python pump, so nothing surfaces to the caller. Without a
    record, a deployment that has quietly stopped taking the fast path is
    indistinguishable from one that never had it, which is the question an
    operator actually asks. Logged at debug level because this is a per-hop
    internal routing decision, not a fault.

    The log record is written first and the pump event second, so a registered
    observer can neither suppress nor precede the record operators already
    filter on; the counter supplements that record rather than replacing it.

    Examples
    --------
    Capture the reason a hop declined the fast path::

        with caplog.at_level(logging.DEBUG, logger="cuprum._pipeline_streams"):
            _log_rust_pump_declined(
                RustPumpDeclineReason.BLOCKING_MODE_UNAVAILABLE,
            )
        assert caplog.records[0].__dict__["cuprum_reason"] == (
            "blocking_mode_unavailable"
        )

    """
    _LOGGER.debug(
        "Inter-stage hop declined the Rust pump (%s); using the Python pump",
        reason.value,
        extra={"cuprum_action": "rust_pump_declined", "cuprum_reason": reason.value},
    )
    _emit_pump_event(PumpEvent(phase="declined", reason=reason))


@dc.dataclass(slots=True)
class _PumpStreamDispatchTestHooks:
    """Test-only overrides for stream dispatch."""

    force_fd_extraction_failure: bool = False
    on_rust_fd_path_attempt: cabc.Callable[[], None] | None = None
    python_pump: (
        cabc.Callable[
            [asyncio.StreamReader | None, asyncio.StreamWriter | None],
            cabc.Awaitable[None],
        ]
        | None
    ) = None


_PUMP_STREAM_DISPATCH_TEST_HOOKS = _PumpStreamDispatchTestHooks()


def configure_pump_stream_dispatch_for_testing(
    *,
    force_fd_extraction_failure: bool = False,
    on_rust_fd_path_attempt: cabc.Callable[[], None] | None = None,
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
    _PUMP_STREAM_DISPATCH_TEST_HOOKS.python_pump = python_pump


def reset_pump_stream_dispatch_for_testing() -> None:
    """Reset ``_pump_stream_dispatch`` test hooks to defaults."""
    configure_pump_stream_dispatch_for_testing()


async def _run_rust_pump(
    *,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
    reader_fd: int,
    writer_fd: int,
) -> bool:
    """Run the Rust pump for one pipe hop; return ``True`` when handled."""
    handled = await _pump_over_raw_fds(
        reader=reader,
        writer=writer,
        reader_fd=reader_fd,
        writer_fd=writer_fd,
    )
    if not handled:
        return False
    # The Rust pump closes the writer FD on return (drop semantics).
    # Suppress OSError so the asyncio transport close does not raise
    # EBADF when the descriptor is already gone — but record it, since the
    # expected EBADF and a close that failed for some other reason are
    # otherwise the same silence.
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
    """Drive the Rust pump over the raw FDs, managing the FD lifecycle.

    Pauses the reader transport (always resuming on exit via ``_paused_reader``)
    and switches the FDs to blocking mode under a ``_BlockingModeGuard`` that
    restores their prior mode. Returns ``False`` — the signal to fall back to
    the Python pump — when the FDs cannot be made blocking; the writer close is
    left to the caller.

    Examples
    --------
    A successful hop reports that Rust handled it, leaving the caller to close
    the writer::

        handled = await _pump_over_raw_fds(
            reader=reader,
            writer=writer,
            reader_fd=reader_fd,
            writer_fd=writer_fd,
        )
        assert handled is True

    When the descriptors cannot be switched to blocking mode the call reports
    ``False`` instead of raising, and the reader transport has already been
    resumed, so the caller falls back to the Python pump::

        handled = await _pump_over_raw_fds(
            reader=reader,
            writer=writer,
            reader_fd=reader_fd,
            writer_fd=writer_fd,
        )
        if not handled:
            await _run_python_pump(reader, writer)

    """
    with _paused_reader(reader) as may_hand_off:
        if not may_hand_off:
            # Pausing failed, so asyncio may still be consuming the reader.
            # Fall back rather than race it for the descriptor.
            _log_rust_pump_declined(RustPumpDeclineReason.READER_PAUSE_FAILED)
            return False

        # Flush any bytes asyncio already buffered in the StreamReader before
        # the Rust pump takes over the raw file descriptor.
        await _drain_reader_buffer(reader, writer)

        try:
            guard = _BlockingModeGuard.engage(reader_fd=reader_fd, writer_fd=writer_fd)
        except (OSError, ValueError):
            _log_rust_pump_declined(RustPumpDeclineReason.BLOCKING_MODE_UNAVAILABLE)
            return False

        await _await_rust_pump(guard, reader_fd=reader_fd, writer_fd=writer_fd)
    return True


async def _await_rust_pump(
    guard: _BlockingModeGuard,
    *,
    reader_fd: int,
    writer_fd: int,
) -> None:
    """Run the Rust pump in an executor, restoring the FDs only once it ends.

    Cancelling the awaiting task cannot interrupt the worker thread, which
    still owns both raw descriptors. Restoring their blocking mode — or letting
    ``_paused_reader`` resume the transport — while that thread is mid-transfer
    would race it, so on cancellation this waits for the worker to return
    before the descriptors are touched.
    """
    from cuprum._streams_rs import rust_pump_stream

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, rust_pump_stream, reader_fd, writer_fd)
    try:
        # Shielded so cancelling this task does not mark the executor future
        # cancelled while its worker thread is still running.
        await asyncio.shield(future)
    except asyncio.CancelledError:
        # Drain the worker before propagating: the descriptors must outlive it.
        # A further cancellation interrupts the wait itself, so loop until the
        # worker has actually finished — a single suppressed wait survives only
        # one extra cancellation, and returning early would restore the
        # descriptors while the worker still owns them, which is the precise
        # hazard this drain exists to prevent. Only CancelledError is
        # suppressed: KeyboardInterrupt and SystemExit must still travel, or a
        # shutdown signal arriving here would be swallowed.
        while not future.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait([future])
        _report_pump_outcome_after_cancel(future)
        raise
    finally:
        guard.restore()


def _report_pump_outcome_after_cancel(future: asyncio.Future[int]) -> None:
    """Retrieve a drained pump's outcome, recording any failure it carried.

    ``asyncio.wait`` never retrieves a future's outcome, so a pump that failed
    on a cancelled hop would resurface at garbage collection as an
    unretrieved-exception warning, detached from the hop that caused it.

    The caller is told about the cancellation, which is what it asked for, so
    the pump's own error would otherwise vanish entirely. Record it at debug
    level — the same convention as ``_log_rust_pump_declined`` — so a failure
    masked by cancellation stays diagnosable.

    Examples
    --------
    Consume the outcome of a worker that failed while its hop was cancelled::

        with caplog.at_level(logging.DEBUG, logger="cuprum._pipeline_streams"):
            _report_pump_outcome_after_cancel(future)
        assert caplog.records[0].__dict__["cuprum_action"] == (
            "rust_pump_failed_after_cancel"
        )

    """
    if future.cancelled():
        return
    error = future.exception()
    if error is None:
        return
    # `exc_info` rather than `%r`: this record exists so a cancellation-masked
    # failure stays diagnosable, and the type and message alone do not say
    # where in the pump it happened.
    _LOGGER.debug(
        "Rust pump failed while its hop was being cancelled",
        exc_info=error,
        extra={"cuprum_action": "rust_pump_failed_after_cancel"},
    )
    # The event carries no error detail. The traceback above is the diagnostic;
    # an exception type on a metric label is a series per failure mode the
    # library does not control. Emission cannot raise past here — this runs
    # inside cancellation unwinding, and a raise would displace the
    # `CancelledError` the caller is owed. See `_emit_pump_event`.
    _emit_pump_event(PumpEvent(phase="failed_after_cancel"))


async def _drain_reader_buffer(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
) -> None:
    """Flush bytes already buffered in *reader* to *writer*."""
    # StreamReader._buffer is a CPython-private bytearray populated by the
    # event loop before our coroutine is scheduled.  The Rust pump reads from
    # the raw FD and would skip those bytes, so we flush them here first.
    # getattr gracefully degrades to a no-op if the attribute is absent (e.g.
    # on PyPy or future CPython versions that rename or remove _buffer).
    buffered: bytearray | None = getattr(reader, "_buffer", None)
    if not buffered:
        return
    if writer is not None:
        try:
            writer.write(bytes(buffered))
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
    # Clear unconditionally so the Rust pump does not re-read stale data.
    buffered.clear()


async def _run_python_pump(
    reader: asyncio.StreamReader | None,
    writer: asyncio.StreamWriter | None,
) -> None:
    """Run the configured Python pump implementation."""
    python_pump = _PUMP_STREAM_DISPATCH_TEST_HOOKS.python_pump
    if python_pump is not None:
        await python_pump(reader, writer)
        return
    await _pump_stream(reader, writer)


async def _try_rust_pump(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
) -> bool:
    """Attempt to route the pipe hop through the Rust pump.

    Returns ``True`` when Rust handled the pump, ``False`` to fall back
    to the Python implementation.
    """
    rust_fd_attempt_hook = _PUMP_STREAM_DISPATCH_TEST_HOOKS.on_rust_fd_path_attempt
    if rust_fd_attempt_hook is not None:
        rust_fd_attempt_hook()

    if _PUMP_STREAM_DISPATCH_TEST_HOOKS.force_fd_extraction_failure:
        return False

    reader_fd = _extract_stream_fd(reader)
    writer_fd = _extract_stream_fd(writer)

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
) -> None:
    """Route inter-stage pump to the Rust or Python implementation."""
    if reader is None:
        await _run_python_pump(reader, writer)
        return

    backend = get_stream_backend()
    if backend is StreamBackend.RUST and await _try_rust_pump(reader, writer):
        return

    await _run_python_pump(reader, writer)
