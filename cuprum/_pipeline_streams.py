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

import dataclasses as dc
import functools
import logging
import typing as typ

from cuprum._backend import StreamBackend, get_stream_backend
from cuprum._pipeline_pipe_tasks import (
    _create_pipe_tasks as _create_pipe_tasks_with_context,
)
from cuprum._pipeline_stream_fds import (
    _extract_stream_fd,
    _pause_reader_transport,
    _resume_reader_transport,
    _suppressed_teardown_failure,
)
from cuprum._pipeline_stream_native_cleanup import (
    _DEFAULT_NATIVE_PUMP_CLEANUP_GRACE,
    _create_rust_pump_state,
    _run_rust_pump_with_blocking_fds,
    _RustPumpHandoff,
)
from cuprum._streams import _close_stream_writer, _pump_stream
from cuprum.pump_events import PumpEvent, RustPumpDeclineReason
from cuprum.pump_observation import _emit_pump_event

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc

    from cuprum._pipeline_types import _StageObservation


_LOGGER = logging.getLogger(__name__)


def _log_rust_pump_declined(reason: RustPumpDeclineReason) -> None:
    """Record the reason an inter-stage hop falls back to Python pumping."""
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


async def _run_rust_pump(
    *,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
    handoff: _RustPumpHandoff,
) -> bool:
    """Run the Rust pump while the executor future owns native cleanup."""
    handled = await _pump_over_raw_fds(
        reader=reader,
        writer=writer,
        handoff=handoff,
    )
    if not handled:
        return False
    # Rust closed only its duplicate, so the transport descriptor is still
    # valid: close it through asyncio to signal EOF downstream.
    with _suppressed_teardown_failure(_LOGGER, "writer_close", OSError):
        await _close_stream_writer(writer)
    return True


async def _pump_over_raw_fds(
    *,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
    handoff: _RustPumpHandoff,
) -> bool:
    """Transfer a hop after acquiring the reader and descriptor hand-off."""
    # Flush any bytes asyncio already buffered in the StreamReader
    # before the Rust pump takes over the raw file descriptor.
    reader_pause = _pause_reader_transport(reader)
    if not reader_pause.may_hand_off:
        _log_rust_pump_declined(
            reader_pause.decline_reason or RustPumpDeclineReason.READER_PAUSE_FAILED,
        )
        return False
    try:
        await _drain_reader_buffer(reader, writer)
    except BaseException:
        _resume_reader_transport(reader_pause.resume)
        raise

    try:
        # These duplicates outlive the caller-facing task. They carry the
        # blocking-mode state that only the completion callback may restore
        # after native I/O has stopped.
        state = _create_rust_pump_state(handoff, reader_pause.resume)
    except (OSError, ValueError):
        _resume_reader_transport(reader_pause.resume)
        _log_rust_pump_declined(RustPumpDeclineReason.BLOCKING_MODE_UNAVAILABLE)
        return False

    await _run_rust_pump_with_blocking_fds(state=state)
    return True


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
    *,
    cleanup_grace_s: float = _DEFAULT_NATIVE_PUMP_CLEANUP_GRACE,
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
        handoff=_RustPumpHandoff(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            cleanup_grace_s=cleanup_grace_s,
        ),
    )


async def _pump_stream_dispatch(
    reader: asyncio.StreamReader | None,
    writer: asyncio.StreamWriter | None,
    *,
    cleanup_grace_s: float = _DEFAULT_NATIVE_PUMP_CLEANUP_GRACE,
) -> None:
    """Route inter-stage pump to the Rust or Python implementation."""
    if reader is None:
        await _run_python_pump(reader, writer)
        return

    backend = get_stream_backend()
    if backend is StreamBackend.RUST and await _try_rust_pump(
        reader,
        writer,
        cleanup_grace_s=cleanup_grace_s,
    ):
        return

    await _run_python_pump(reader, writer)


def _create_pipe_tasks(
    processes: list[asyncio.subprocess.Process],
    observations: tuple[_StageObservation, ...] = (),
    native_pump_cleanup_grace: float = _DEFAULT_NATIVE_PUMP_CLEANUP_GRACE,
) -> list[asyncio.Task[None]]:
    """Create streaming tasks between adjacent pipeline stages."""
    return _create_pipe_tasks_with_context(
        processes,
        observations,
        functools.partial(
            _pump_stream_dispatch,
            cleanup_grace_s=native_pump_cleanup_grace,
        ),
    )
