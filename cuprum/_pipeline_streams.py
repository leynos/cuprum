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
import functools
import os
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
    _close_rust_writer_fd,
    _extract_stream_fd,
    _pause_reader_transport,
    _resume_reader_transport,
)
from cuprum._pipeline_stream_results import (
    _cancel_stream_tasks as _cancel_stream_tasks,
)
from cuprum._pipeline_stream_results import (
    _collect_pipe_results as _collect_pipe_results,
)
from cuprum._pipeline_stream_results import (
    _flatten_stream_tasks as _flatten_stream_tasks,
)
from cuprum._pipeline_stream_results import (
    _gather_optional_text_tasks as _gather_optional_text_tasks,
)
from cuprum._pipeline_stream_results import (
    _reconcile_pipe_tasks as _reconcile_pipe_tasks,
)
from cuprum._pipeline_stream_results import (
    _surface_unexpected_pipe_failures as _surface_unexpected_pipe_failures,
)
from cuprum._streams import _close_stream_writer, _pump_stream

if typ.TYPE_CHECKING:
    import collections.abc as cabc


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


@dc.dataclass(frozen=True, slots=True)
class _RustPumpState:
    """Capture transport-owned state that native pumping must restore."""

    reader_fd: int
    writer_fd: int
    blocking_mode_guard: _BlockingModeGuard
    resume_reader: cabc.Callable[[], None] | None


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
    _PUMP_STREAM_DISPATCH_TEST_HOOKS.force_fd_extraction_failure = False
    _PUMP_STREAM_DISPATCH_TEST_HOOKS.on_rust_fd_path_attempt = None
    _PUMP_STREAM_DISPATCH_TEST_HOOKS.python_pump = None


def _restore_rust_pump_state(state: _RustPumpState) -> None:
    """Restore pipe state before returning reader transport control to asyncio."""
    state.blocking_mode_guard.restore()
    _resume_reader_transport(state.resume_reader)


def _complete_rust_pump(
    completed: asyncio.Future[object],
    *,
    cleanup_complete: asyncio.Future[None],
    rust_writer_fd: int,
    state: _RustPumpState,
) -> None:
    """Release native-pump resources after its executor worker settles."""
    try:
        if not completed.cancelled():
            completed.exception()
    finally:
        _close_rust_writer_fd(rust_writer_fd)
        _restore_rust_pump_state(state)
        if not cleanup_complete.done():
            cleanup_complete.set_result(None)


async def _run_rust_pump_with_blocking_fds(
    *,
    state: _RustPumpState,
) -> None:
    """Run the native pump while its executor future owns cleanup."""
    from cuprum._streams_rs import rust_pump_stream

    # Rust consumes this duplicate; asyncio keeps the transport descriptor.
    rust_writer_fd = os.dup(state.writer_fd)
    loop = asyncio.get_running_loop()
    cleanup_complete = loop.create_future()
    try:
        native_pump = loop.run_in_executor(
            None,
            rust_pump_stream,
            state.reader_fd,
            rust_writer_fd,
        )
    except BaseException:
        _close_rust_writer_fd(rust_writer_fd)
        _restore_rust_pump_state(state)
        raise
    native_pump.add_done_callback(
        functools.partial(
            _complete_rust_pump,
            cleanup_complete=cleanup_complete,
            rust_writer_fd=rust_writer_fd,
            state=state,
        )
    )
    try:
        await asyncio.shield(native_pump)
    except asyncio.CancelledError:
        raise
    except BaseException:
        await asyncio.shield(cleanup_complete)
        raise
    await asyncio.shield(cleanup_complete)


async def _run_rust_pump(
    *,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
    reader_fd: int,
    writer_fd: int,
) -> bool:
    """Run the Rust pump while the executor future owns native cleanup."""
    # Flush any bytes asyncio already buffered in the StreamReader
    # before the Rust pump takes over the raw file descriptor.
    resume_reader = _pause_reader_transport(reader)
    try:
        await _drain_reader_buffer(reader, writer)
    except BaseException:
        _resume_reader_transport(resume_reader)
        raise

    try:
        blocking_mode_guard = _BlockingModeGuard.engage(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
        )
    except (OSError, ValueError):
        _resume_reader_transport(resume_reader)
        return False

    state = _RustPumpState(
        reader_fd=reader_fd,
        writer_fd=writer_fd,
        blocking_mode_guard=blocking_mode_guard,
        resume_reader=resume_reader,
    )
    await _run_rust_pump_with_blocking_fds(state=state)
    # Rust closed only its duplicate, so the transport descriptor is still
    # valid: close it through asyncio to signal EOF downstream.
    with contextlib.suppress(OSError):
        await _close_stream_writer(writer)
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
) -> bool:
    """Attempt to route the pipe hop through the Rust pump."""
    rust_fd_attempt_hook = _PUMP_STREAM_DISPATCH_TEST_HOOKS.on_rust_fd_path_attempt
    if rust_fd_attempt_hook is not None:
        rust_fd_attempt_hook()

    if _PUMP_STREAM_DISPATCH_TEST_HOOKS.force_fd_extraction_failure:
        return False

    reader_fd = _extract_stream_fd(reader)
    writer_fd = _extract_stream_fd(writer)

    if reader_fd is None or writer_fd is None:
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


def _create_pipe_tasks(
    processes: list[asyncio.subprocess.Process],
) -> list[asyncio.Task[None]]:
    """Create streaming tasks between adjacent pipeline stages."""
    return [
        asyncio.create_task(
            _pump_stream_dispatch(
                processes[idx].stdout,
                processes[idx + 1].stdin,
            ),
        )
        for idx in range(len(processes) - 1)
    ]
