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
import dataclasses as dc
import functools
import logging
import os
import typing as typ

from cuprum import _pipeline_stream_cleanup_observation as _pump_obs
from cuprum._backend import StreamBackend, get_stream_backend
from cuprum._pipeline_native_pump_types import (
    _DEFAULT_NATIVE_PUMP_CLEANUP_GRACE,
    _RustPumpBlockingModeError,
    _RustPumpHandoff,
    _RustPumpStateDuplicationError,
)
from cuprum._pipeline_pipe_tasks import (
    _create_pipe_tasks as _create_pipe_tasks_with_context,
)
from cuprum._pipeline_stream_fds import (
    _extract_stream_fd,
    _pause_reader_transport,
    _ReaderPause,
    _resume_reader_transport,
    _suppressed_teardown_failure,
)
from cuprum._pipeline_stream_native_cleanup import (
    _create_rust_pump_state,
    _run_rust_pump_with_blocking_fds,
)
from cuprum._streams import _close_stream_writer, _pump_stream
from cuprum._streams_pump import _current_read_size
from cuprum.pump_events import RustPumpDeclineReason, RustPumpHandoffOutcome
from cuprum.pump_observation import _emit_rust_pump_handoff_outcome

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import _StageObservation


_LOGGER = logging.getLogger(__name__)


def _log_rust_pump_declined(reason: RustPumpDeclineReason) -> None:
    """Record the reason an inter-stage hop falls back to Python pumping."""
    _pump_obs._log_native_pump_declined(_LOGGER, reason)


def _native_pump_supported_on_platform() -> bool:
    """Return whether the native Rust pump is safe for this platform.

    Windows ``ProactorEventLoop`` subprocess pipes use overlapped handles, but
    the Rust extension currently performs synchronous ``std::fs::File`` I/O.
    Do not give native pumping those handles until it implements true
    overlapped I/O.

    Returns
    -------
    bool
        Whether this operating system can use synchronous native pipe I/O.
    """
    return os.name != "nt"


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
    reader_pause = _pause_reader_transport(reader)
    if (
        reader_pause.closing_transport
        and _PUMP_STREAM_DISPATCH_TEST_HOOKS.raw_fd_extractor is not None
    ):
        # The dispatch seam supplies descriptors it owns, so a reader whose
        # transport is already closing no longer invalidates this hand-off.
        # Nothing was paused, so the permitted verdict carries no resume.
        reader_pause = _ReaderPause(may_hand_off=True)
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
    except _RustPumpStateDuplicationError as failure:
        _resume_reader_transport(reader_pause.resume)
        _pump_obs._log_native_pump_handoff_failed(
            _LOGGER,
            "duplicate_writer",
            failure.error,
        )
        _emit_rust_pump_handoff_outcome(RustPumpHandoffOutcome.DUPLICATE_WRITER_FAILED)
        raise failure.error from failure
    except _RustPumpBlockingModeError:
        _resume_reader_transport(reader_pause.resume)
        _log_rust_pump_declined(RustPumpDeclineReason.BLOCKING_MODE_UNAVAILABLE)
        _emit_rust_pump_handoff_outcome(RustPumpHandoffOutcome.BLOCKING_SETUP_FAILED)
        return False

    await _run_rust_pump_with_blocking_fds(state=state)
    return True


async def _settle_paused_reader_callbacks() -> None:
    """Let callbacks queued before a transport pause finish filling its buffer."""
    loop = asyncio.get_running_loop()
    settled = loop.create_future()
    loop.call_soon(settled.set_result, None)
    await settled


async def _drain_reader_buffer(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
) -> None:
    """Deliver asyncio-buffered bytes before handing its descriptor to Rust."""
    await _settle_paused_reader_callbacks()
    # The raw descriptor cannot replay bytes the StreamReader already owns.
    # ``getattr`` leaves the native path available on compatible reader types
    # without this CPython implementation detail.
    buffered: bytearray | None = getattr(reader, "_buffer", None)
    if not buffered:
        return
    try:
        if writer is not None:
            writer.write(bytes(buffered))
            await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        # A closed downstream has a known discard outcome; the native worker
        # still drains the unread descriptor so the upstream cannot block.
        pass
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
    *,
    cleanup_grace_s: float = _DEFAULT_NATIVE_PUMP_CLEANUP_GRACE,
) -> bool:
    """Attempt to route the pipe hop through the Rust pump."""
    if not _native_pump_supported_on_platform():
        _log_rust_pump_declined(RustPumpDeclineReason.PLATFORM_UNSUPPORTED)
        return False

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
    read_size: int | None = None,
) -> None:
    """Route inter-stage pump to the Rust or Python implementation."""
    active_read_size = _current_read_size() if read_size is None else read_size
    if reader is None:
        await _run_python_pump(reader, writer, read_size=active_read_size)
        return

    backend = get_stream_backend()
    if backend is StreamBackend.RUST and await _try_rust_pump(
        reader,
        writer,
        cleanup_grace_s=cleanup_grace_s,
    ):
        return

    await _run_python_pump(reader, writer, read_size=active_read_size)


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
            read_size=_current_read_size(),
        ),
    )
