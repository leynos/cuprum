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


def _fd_from_transport(transport: object | None) -> int | None:
    """Extract a raw FD via ``transport.get_extra_info('pipe').fileno()``."""
    get_extra = getattr(transport, "get_extra_info", None)
    if get_extra is None:
        return None
    pipe: object | None = get_extra("pipe")
    fileno = getattr(pipe, "fileno", None) if pipe is not None else None
    if fileno is None:
        return None
    try:
        return int(fileno())
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _extract_stream_fd(
    stream: asyncio.StreamReader | asyncio.StreamWriter | None,
) -> int | None:
    """Extract a raw FD from an asyncio stream via its transport."""
    if stream is None:
        return None
    transport = getattr(stream, "transport", None)
    if transport is None:
        transport = getattr(stream, "_transport", None)
    return _fd_from_transport(transport)


def _pause_reader_transport(
    reader: asyncio.StreamReader,
) -> cabc.Callable[[], None] | None:
    """Pause reader transport callbacks while Rust pump owns the raw FD."""
    transport = getattr(reader, "transport", None)
    if transport is None:
        transport = getattr(reader, "_transport", None)
    pause_reading = getattr(transport, "pause_reading", None)
    resume_reading = getattr(transport, "resume_reading", None)
    if not callable(pause_reading) or not callable(resume_reading):
        return None
    try:
        pause_reading()
    except (RuntimeError, OSError):
        return None

    def _resume() -> None:
        """Resume the paused reader transport, ignoring teardown errors."""
        with contextlib.suppress(RuntimeError, OSError):
            resume_reading()

    return _resume


def _set_stream_fds_blocking(*, reader_fd: int, writer_fd: int) -> tuple[bool, bool]:
    """Switch pipe FDs to blocking mode and return their prior state."""
    reader_was_blocking = os.get_blocking(reader_fd)
    writer_was_blocking = os.get_blocking(writer_fd)
    reader_changed = False
    try:
        if not reader_was_blocking:
            os.set_blocking(reader_fd, True)
            reader_changed = True
        if not writer_was_blocking:
            os.set_blocking(writer_fd, True)
    except OSError:
        if reader_changed:
            with contextlib.suppress(OSError, ValueError):
                os.set_blocking(reader_fd, reader_was_blocking)
        raise
    return reader_was_blocking, writer_was_blocking


def _restore_stream_fd_blocking(
    *,
    reader_fd: int,
    writer_fd: int,
    reader_was_blocking: bool,
    writer_was_blocking: bool,
) -> None:
    """Restore pipe FD blocking mode captured before Rust pumping."""
    with contextlib.suppress(OSError, ValueError):
        os.set_blocking(reader_fd, reader_was_blocking)
    with contextlib.suppress(OSError, ValueError):
        os.set_blocking(writer_fd, writer_was_blocking)


async def _run_rust_pump(
    *,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
    reader_fd: int,
    writer_fd: int,
) -> bool:
    """Run the Rust pump for one pipe hop; return ``True`` when handled.

    ``rust_pump_stream`` consumes the writer descriptor it is given and closes
    it on return, which is how it signals EOF downstream. asyncio owns the
    transport's own descriptor and closes it through the transport, so the two
    cannot be the same descriptor: handing the transport's descriptor to Rust
    closes it behind asyncio's back and every later transport operation fails
    with ``EBADF``. Rust is therefore given a duplicate, and asyncio keeps sole
    ownership of the descriptor its transport holds.

    Returns
    -------
    bool
        ``True`` when the native pump handled the hop, ``False`` when the
        caller should fall back to the Python pump.
    """
    # Flush any bytes asyncio already buffered in the StreamReader
    # before the Rust pump takes over the raw file descriptor.
    resume_reader = _pause_reader_transport(reader)
    try:
        await _drain_reader_buffer(reader, writer)
    except Exception:
        if resume_reader is not None:
            resume_reader()
        raise

    from cuprum._streams_rs import rust_pump_stream

    try:
        # Rust consumes this duplicate; the original stays asyncio's to close.
        rust_writer_fd = os.dup(writer_fd)
    except OSError:
        if resume_reader is not None:
            resume_reader()
        return False

    try:
        reader_was_blocking, writer_was_blocking = _set_stream_fds_blocking(
            reader_fd=reader_fd,
            writer_fd=rust_writer_fd,
        )
    except OSError:
        with contextlib.suppress(OSError):
            os.close(rust_writer_fd)
        if resume_reader is not None:
            resume_reader()
        return False

    loop = asyncio.get_running_loop()
    try:
        # Ownership of ``rust_writer_fd`` passes to Rust here: it closes the
        # duplicate on return, so Python must not close it again.
        await loop.run_in_executor(
            None,
            rust_pump_stream,
            reader_fd,
            rust_writer_fd,
        )
    finally:
        # Restore through the original descriptor. The duplicate may already be
        # closed by Rust, and blocking mode lives on the shared open file
        # description, so the original restores the same state.
        _restore_stream_fd_blocking(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            reader_was_blocking=reader_was_blocking,
            writer_was_blocking=writer_was_blocking,
        )
        if resume_reader is not None:
            resume_reader()
    # Rust closed only its duplicate, so the transport descriptor is still
    # valid: close it through asyncio to signal EOF downstream.
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
