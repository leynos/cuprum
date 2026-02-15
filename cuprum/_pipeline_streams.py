"""Stream coordination for pipeline execution."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import sys
import typing as typ

from cuprum._backend import StreamBackend, get_stream_backend
from cuprum._streams import (
    _close_stream_writer,
    _consume_stream,
    _pump_stream,
    _StreamConfig,
)

if typ.TYPE_CHECKING:
    from cuprum._pipeline_internals import _StageObservation
    from cuprum.sh import ExecutionContext


@dc.dataclass(frozen=True, slots=True)
class _PipelineRunConfig:
    ctx: ExecutionContext
    capture: bool
    echo: bool
    timeout: float | None
    stdout_sink: typ.IO[str]
    stderr_sink: typ.IO[str]

    @property
    def capture_or_echo(self) -> bool:
        return self.capture or self.echo

    @property
    def stream_config(self) -> _StreamConfig:
        return _StreamConfig(
            capture_output=self.capture,
            echo_output=self.echo,
            sink=self.stdout_sink,
            encoding=self.ctx.encoding,
            errors=self.ctx.errors,
        )


def _prepare_pipeline_config(
    *,
    capture: bool,
    echo: bool,
    timeout: float | None,
    context: ExecutionContext | None,
) -> _PipelineRunConfig:
    """Normalise runtime options for pipeline execution."""
    from cuprum._pipeline_internals import _sh_module

    sh = _sh_module()
    ctx = context or sh.ExecutionContext()
    stdout_sink = ctx.stdout_sink if ctx.stdout_sink is not None else sys.stdout
    stderr_sink = ctx.stderr_sink if ctx.stderr_sink is not None else sys.stderr
    return _PipelineRunConfig(
        ctx=ctx,
        capture=capture,
        echo=echo,
        timeout=timeout,
        stdout_sink=stdout_sink,
        stderr_sink=stderr_sink,
    )


@dc.dataclass(frozen=True, slots=True)
class _StageStreamConfig:
    """Stream file descriptor configuration for a pipeline stage."""

    stdin: int
    stdout: int
    stderr: int


def _get_stage_stream_fds(
    idx: int,
    last_idx: int,
    *,
    capture_or_echo: bool,
) -> _StageStreamConfig:
    """Determine stream file descriptors for a pipeline stage.

    First stage reads from DEVNULL; intermediate stages use pipes for stdin.
    stdout is piped for intermediate stages or when capturing. stderr is piped
    only when capturing or echoing.
    """
    stdin = asyncio.subprocess.DEVNULL if idx == 0 else asyncio.subprocess.PIPE
    stdout = (
        asyncio.subprocess.PIPE
        if idx != last_idx or capture_or_echo
        else asyncio.subprocess.DEVNULL
    )
    stderr = asyncio.subprocess.PIPE if capture_or_echo else asyncio.subprocess.DEVNULL
    return _StageStreamConfig(stdin=stdin, stdout=stdout, stderr=stderr)


def _create_stage_capture_tasks(
    process: asyncio.subprocess.Process,
    config: _PipelineRunConfig,
    *,
    is_last_stage: bool,
    observation: _StageObservation,
) -> tuple[asyncio.Task[str | None] | None, asyncio.Task[str | None] | None]:
    """Create stderr and stdout capture tasks for a pipeline stage.

    Returns (stderr_task, stdout_task). stderr is captured for all stages when
    capture_or_echo is enabled. stdout is only captured for the final stage.
    """
    stderr_task: asyncio.Task[str | None] | None = None
    stdout_task: asyncio.Task[str | None] | None = None

    if not config.capture_or_echo:
        return stderr_task, stdout_task

    stderr_on_line: typ.Callable[[str], None] | None = None
    if observation.hooks.observe_hooks:

        def stderr_on_line(line: str) -> None:
            from cuprum._pipeline_internals import _EventDetails

            observation.emit(
                "stderr",
                _EventDetails(pid=process.pid, line=line),
            )

    stderr_task = asyncio.create_task(
        _consume_stream(
            process.stderr,
            dc.replace(config.stream_config, sink=config.stderr_sink),
            on_line=stderr_on_line,
        ),
    )

    if not is_last_stage:
        return stderr_task, stdout_task

    stdout_on_line: typ.Callable[[str], None] | None = None
    if observation.hooks.observe_hooks:

        def stdout_on_line(line: str) -> None:
            from cuprum._pipeline_internals import _EventDetails

            observation.emit(
                "stdout",
                _EventDetails(pid=process.pid, line=line),
            )

    stdout_task = asyncio.create_task(
        _consume_stream(
            process.stdout,
            config.stream_config,
            on_line=stdout_on_line,
        ),
    )

    return stderr_task, stdout_task


def _fd_from_transport(transport: object | None) -> int | None:
    """Extract a raw file descriptor from an asyncio transport.

    Walks the chain ``transport.get_extra_info('pipe').fileno()`` and
    returns the integer file descriptor, or ``None`` when any step in the
    chain is unavailable or raises.

    Returns
    -------
    int or None
        The file descriptor, or ``None`` on failure.
    """
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
    """Extract a raw file descriptor from an asyncio stream object.

    Looks for the underlying transport via the public ``transport``
    attribute first (``StreamWriter``), then falls back to the private
    ``_transport`` attribute (``StreamReader``).

    Returns
    -------
    int or None
        The file descriptor, or ``None`` when the stream is ``None`` or
        the transport does not expose a pipe object.
    """
    if stream is None:
        return None
    transport = getattr(stream, "transport", None)
    if transport is None:
        transport = getattr(stream, "_transport", None)
    return _fd_from_transport(transport)


def _extract_reader_fd(reader: asyncio.StreamReader | None) -> int | None:
    """Extract the raw file descriptor from an asyncio StreamReader.

    Returns
    -------
    int or None
        The underlying file descriptor, or ``None`` when the reader is
        ``None`` or the transport does not expose a pipe object.
    """
    return _extract_stream_fd(reader)


def _extract_writer_fd(writer: asyncio.StreamWriter | None) -> int | None:
    """Extract the raw file descriptor from an asyncio StreamWriter.

    Returns
    -------
    int or None
        The underlying file descriptor, or ``None`` when the writer is
        ``None`` or the transport does not expose a pipe object.
    """
    return _extract_stream_fd(writer)


async def _drain_reader_buffer(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter | None,
) -> None:
    """Flush bytes already buffered in *reader* to *writer*.

    When the Rust pump takes over the raw file descriptor it bypasses the
    asyncio ``StreamReader`` internal buffer.  Any data that asyncio has
    already read from the OS but not yet consumed would be silently lost.
    Calling this helper before the Rust pump ensures those bytes are
    forwarded to the writer first.

    Parameters
    ----------
    reader : asyncio.StreamReader
        The stream reader whose internal buffer may contain pre-read data.
    writer : asyncio.StreamWriter or None
        The destination writer.  If ``None`` no data is written.
    """
    buffered: bytearray | None = getattr(reader, "_buffer", None)
    if not buffered or writer is None:
        return
    writer.write(bytes(buffered))
    await writer.drain()
    buffered.clear()


async def _pump_stream_dispatch(
    reader: asyncio.StreamReader | None,
    writer: asyncio.StreamWriter | None,
) -> None:
    """Route inter-stage pump to the Rust or Python implementation.

    When the resolved backend is ``RUST`` and both file descriptors are
    extractable from the asyncio transports, the Rust extension runs outside
    the GIL via ``loop.run_in_executor()``.  Otherwise the pure Python
    ``_pump_stream`` is used.
    """
    if reader is None:
        await _pump_stream(reader, writer)
        return

    backend = get_stream_backend()
    if backend is StreamBackend.RUST:
        reader_fd = _extract_reader_fd(reader)
        writer_fd = _extract_writer_fd(writer)
        if reader_fd is not None and writer_fd is not None:
            # Flush any bytes asyncio already buffered in the StreamReader
            # before the Rust pump takes over the raw file descriptor.
            await _drain_reader_buffer(reader, writer)

            from cuprum._streams_rs import rust_pump_stream

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                rust_pump_stream,
                reader_fd,
                writer_fd,
            )
            # The Rust pump closes the writer FD on return (drop semantics).
            # Suppress OSError so the asyncio transport close does not raise
            # EBADF when the descriptor is already gone.
            with contextlib.suppress(OSError):
                await _close_stream_writer(writer)
            return

    await _pump_stream(reader, writer)


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


def _flatten_stream_tasks(
    stderr_tasks: list[asyncio.Task[str | None] | None],
    stdout_task: asyncio.Task[str | None] | None,
) -> list[asyncio.Task[str | None]]:
    """Collect all running stream consumer tasks for cancellation cleanup."""
    tasks = [task for task in stderr_tasks if task is not None]
    if stdout_task is not None:
        tasks.append(stdout_task)
    return tasks


async def _cancel_stream_tasks(
    stderr_tasks: list[asyncio.Task[str | None] | None],
    stdout_task: asyncio.Task[str | None] | None,
) -> None:
    """Cancel stream consumer tasks and await their completion."""
    tasks = _flatten_stream_tasks(stderr_tasks, stdout_task)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _gather_optional_text_tasks(
    tasks: list[asyncio.Task[str | None] | None],
) -> tuple[str | None, ...]:
    """Await optional capture tasks, returning a tuple aligned with inputs."""
    return tuple(
        await asyncio.gather(
            *(
                task if task is not None else asyncio.sleep(0, result=None)
                for task in tasks
            ),
        ),
    )


async def _collect_pipe_results(
    pipe_tasks: list[asyncio.Task[None]],
) -> list[object]:
    """Collect pipe task results, capturing exceptions rather than raising them.

    Uses return_exceptions=True to gather all results including any exceptions
    that occurred during pipe streaming between pipeline stages.
    """
    return list(await asyncio.gather(*pipe_tasks, return_exceptions=True))


def _surface_unexpected_pipe_failures(pipe_results: list[object]) -> None:
    """Raise non-BrokenPipe exceptions from pipe results.

    BrokenPipeError and ConnectionResetError are expected when downstream
    processes terminate early (e.g., head) and should not fail the pipeline.
    Other exceptions indicate genuine failures and must be surfaced.
    """
    for result in pipe_results:
        if isinstance(result, Exception) and not isinstance(
            result,
            (BrokenPipeError, ConnectionResetError),
        ):
            raise result
