"""Pull-style line iteration over one subprocess.

Owns the coordination behind ``SafeCmd.lines()``. The subprocess, its stream
consumers, its deadline, and the teardown rules are the same primitives
``SafeCmd.run()`` uses; the only addition is an :class:`asyncio.Queue` fed by
the composed per-line callback, so a caller iterating lines is decoupled from
the drain without forking the run semantics.

The consumer tasks are registered in ``_RunTaskOwnership.consumers``, so the
shared reconciliation cancels and drains them exactly once on every exit
path: completion, timeout, caller ``break``, generator close, and external
cancellation alike.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import time
import typing as typ

from cuprum._line_callbacks import _LineEmissionContext
from cuprum._pipeline_types import _EventDetails
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._subprocess_execution import (
    _build_stream_config,
    _spawn_subprocess,
    _SubprocessExecution,
)
from cuprum._subprocess_stdin import _spawn_stdin_writer
from cuprum._subprocess_timeout import (
    _emit_exit_event,
    _ExitEventDetails,
    _handle_stream_timeout,
    _handle_subprocess_timeout,
    _SubprocessTimeoutContext,
    _SubprocessTimeoutError,
)
from cuprum._subprocess_wait import (
    _drain_stream_consumers,
    _DrainContext,
    _reconcile_run_tasks,
    _RunTaskOwnership,
    _wait_for_exit_code_within_timeout,
)
from cuprum.lines import LineEvent

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._streams import _StreamConfig
    from cuprum.sh import CommandResult

# The queue item is a line event while the run streams, and the module-level
# sentinel object exactly once, when the coordinator finishes. A dedicated
# sentinel rather than ``None`` keeps a future ``None``-carrying event from
# being ambiguous.
type _LineQueueItem = LineEvent | CommandResult

_LINES_FINALIZATION_ERROR = "line stream finalization failed"
_LINES_LOGGER_NAME = "cuprum.lines"


@dc.dataclass(frozen=True, slots=True)
class _LineStreamRun:
    """The spawned pieces one ``lines()`` iteration coordinates.

    Attributes
    ----------
    process:
        The subprocess whose stdout and stderr feed the queue.
    tasks:
        Ownership of the stdin writer and the stream consumers, so the shared
        reconciliation cancels and drains each exactly once.
    queue:
        The queue every ``LineEvent`` is posted to; the sentinel ends
        iteration.
    started_at:
        Monotonic reference the per-line ``at`` stamps are measured from.

    """

    process: asyncio.subprocess.Process
    tasks: _RunTaskOwnership
    queue: asyncio.Queue[_LineQueueItem]
    started_at: float


def _queue_line_sink(
    queue: asyncio.Queue[_LineQueueItem],
) -> cabc.Callable[[LineEvent], None]:
    """Return a hook that posts one ``LineEvent`` to the queue."""

    def enqueue(event: LineEvent) -> None:
        """Post one stamped line to the consumer queue."""
        queue.put_nowait(event)

    return enqueue


def _spawn_line_consumers(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    stream_config: _StreamConfig,
    *,
    emission: _LineEmissionContext,
) -> tuple[asyncio.Task[str | None], asyncio.Task[str | None]]:
    """Spawn stdout and stderr consumers that feed the line queue.

    The consumer configuration mirrors ``run()``'s streamed path exactly —
    capture and echo are ``execution``'s, the stderr sink override included —
    so iterating lines never silently disables capture or echo.

    Returns
    -------
    tuple[asyncio.Task[str | None], asyncio.Task[str | None]]
        The stdout and stderr consumer tasks, each feeding *emission*'s
        queue sink.
    """
    stdout_on_line = _composed_stream_callback(
        execution, "stdout", dc.replace(emission, stream="stdout")
    )
    stderr_on_line = _composed_stream_callback(
        execution, "stderr", dc.replace(emission, stream="stderr")
    )
    stderr_config = _stderr_stream_config(execution, stream_config)
    return _start_line_consumer_tasks(
        process, stream_config, stderr_config, (stdout_on_line, stderr_on_line)
    )


def _composed_stream_callback(
    execution: _SubprocessExecution,
    stream: typ.Literal["stdout", "stderr"],
    emission: _LineEmissionContext,
) -> cabc.Callable[[str], None] | None:
    """Build the composed per-line callback for one stream."""
    from cuprum._subprocess_execution import _create_stream_callback

    return _create_stream_callback(execution.observation, stream, emission)


def _stderr_stream_config(
    execution: _SubprocessExecution,
    stream_config: _StreamConfig,
) -> _StreamConfig:
    """Return the stderr config with ``run()``'s sink override applied."""
    import sys

    from cuprum.echo_events import EchoStream

    return dc.replace(
        stream_config,
        sink=(
            execution.ctx.stderr_sink
            if execution.ctx.stderr_sink is not None
            else sys.stderr
        ),
        stream=EchoStream.STDERR,
    )


def _start_line_consumer_tasks(
    process: asyncio.subprocess.Process,
    stdout_config: _StreamConfig,
    stderr_config: _StreamConfig,
    callbacks: tuple[
        cabc.Callable[[str], None] | None, cabc.Callable[[str], None] | None
    ],
) -> tuple[asyncio.Task[str | None], asyncio.Task[str | None]]:
    """Start the stdout and stderr consumer tasks with their callbacks."""
    from cuprum._streams import _consume_stream

    stdout_on_line, stderr_on_line = callbacks
    return (
        asyncio.create_task(
            _consume_stream(process.stdout, stdout_config, on_line=stdout_on_line),
        ),
        asyncio.create_task(
            _consume_stream(process.stderr, stderr_config, on_line=stderr_on_line),
        ),
    )


async def _start_line_stream_run(
    execution: _SubprocessExecution,
    queue: asyncio.Queue[_LineQueueItem],
) -> _LineStreamRun:
    """Spawn the subprocess and its queue-feeding consumers.

    Mirrors the streamed-path setup in :func:`_run_subprocess_with_streams`:
    spawn, record the start reference, start the stdin writer, then start the
    consumers with capture and echo as configured.

    Returns
    -------
    _LineStreamRun
        The spawned run, with the process, its task ownership, the queue,
        and the monotonic start reference.
    """
    process = await _spawn_subprocess(execution)
    started_at = time.perf_counter()
    # Frozen dataclass: the stamped execution carries the start reference the
    # composed callbacks read, so it is rebuilt rather than mutated.
    execution = dc.replace(execution, started_at=started_at)
    pid = process.pid
    execution.observation.emit("start", _EventDetails(pid=pid))
    discard_on_cancel = asyncio.Event()
    stream_config = _build_stream_config(execution, discard_on_cancel)
    tasks = _RunTaskOwnership(
        stdin_task=_spawn_stdin_writer(
            process, execution.stdin_data, execution.observation
        ),
        consumers=_spawn_line_consumers(
            process,
            execution,
            stream_config,
            emission=_LineEmissionContext(
                stream="stdout",
                pid=pid,
                on_line=_queue_line_sink(queue),
                started_at=started_at,
            ),
        ),
        discard_on_cancel=discard_on_cancel,
    )
    return _LineStreamRun(
        process=process,
        tasks=tasks,
        queue=queue,
        started_at=started_at,
    )


async def _wait_for_line_stream_exit(
    run: _LineStreamRun,
    execution: _SubprocessExecution,
) -> tuple[int, float, str | None, str | None]:
    """Await process exit, reconciling every task when the wait fails.

    Mirrors ``_wait_for_streamed_process_exit`` so a ``lines()`` run applies
    its deadline, terminates the child, and drains its consumers exactly the
    way ``run()`` does.

    Returns
    -------
    tuple[int, float, str | None, str | None]
        The exit code, exit timestamp, and captured stdout/stderr.
    """
    pid = run.process.pid
    try:
        exit_code, exited_at = await _wait_for_exit_code_within_timeout(
            run.process,
            execution,
        )
    except TimeoutError as exc:
        stdout_text, stderr_text = await _shielded_cleanup(
            _reconcile_run_tasks(
                run.tasks,
                _DrainContext(
                    capture=execution.capture,
                    pid=pid,
                    observation=execution.observation,
                    discard_on_cancel=run.tasks.discard_on_cancel,
                ),
            )
        )
        _handle_stream_timeout(
            exc,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            timeout=execution.timeout,
        )
    except BaseException:
        await _shielded_cleanup(
            _reconcile_run_tasks(
                run.tasks,
                _DrainContext(
                    capture=False,
                    pid=pid,
                    observation=execution.observation,
                    discard_on_cancel=run.tasks.discard_on_cancel,
                ),
            )
        )
        raise
    stdout_text, stderr_text = await _drain_after_exit(run, pid, execution)
    return exit_code, exited_at, stdout_text, stderr_text


async def _drain_after_exit(
    run: _LineStreamRun,
    pid: int | None,
    execution: _SubprocessExecution,
) -> tuple[str | None, str | None]:
    """Await the settled consumers and drain them exactly once on failure."""
    if run.tasks.stdin_task is not None:
        try:
            await run.tasks.stdin_task
        except BaseException:
            await _shielded_cleanup(
                _drain_stream_consumers(
                    run.tasks.consumers,
                    _DrainContext(
                        capture=False,
                        pid=pid,
                        observation=execution.observation,
                        discard_on_cancel=run.tasks.discard_on_cancel,
                    ),
                )
            )
            raise
    try:
        return await asyncio.gather(*run.tasks.consumers)
    except BaseException:
        await _shielded_cleanup(
            _drain_stream_consumers(
                run.tasks.consumers,
                _DrainContext(
                    capture=False,
                    pid=pid,
                    observation=execution.observation,
                    discard_on_cancel=run.tasks.discard_on_cancel,
                ),
            )
        )
        raise


async def _coordinate_line_stream(
    run: _LineStreamRun,
    execution: _SubprocessExecution,
    queue: asyncio.Queue[_LineQueueItem],
    result_future: asyncio.Future[CommandResult],
) -> None:
    """Drive the run to completion, post the sentinel, then resolve the result.

    Every failure path still reconciles the process and the stream tasks
    through the shared helpers before the error is published on the future,
    so the consuming iterator never waits on work that has already ended.
    """
    try:
        result = await _run_to_command_result(run, execution)
    except BaseException as error:  # ruff: ignore[blind-except] - any failure must reach the consumer
        result_future.set_exception(error)
        return
    # The result itself ends iteration; posting it doubles as the sentinel.
    queue.put_nowait(result)
    result_future.set_result(result)


async def _run_to_command_result(
    run: _LineStreamRun,
    execution: _SubprocessExecution,
) -> CommandResult:
    """Run to exit, assemble the ``CommandResult``, and emit the exit event."""
    started_at = run.started_at
    stdout_text: str | None = None
    stderr_text: str | None = None
    try:
        (
            exit_code,
            exited_at,
            stdout_text,
            stderr_text,
        ) = await _wait_for_line_stream_exit(run, execution)
    except (TimeoutError, _SubprocessTimeoutError) as exc:
        _handle_subprocess_timeout(
            _SubprocessTimeoutContext(
                execution=execution,
                process=run.process,
                started_at=started_at,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
            ),
            exc,
        )

    _emit_exit_event(
        execution.observation,
        _ExitEventDetails(
            pid=run.process.pid,
            exit_code=exit_code,
            started_at=started_at,
            exited_at=exited_at,
        ),
    )
    from cuprum._subprocess_context import _sh_module

    return _sh_module().CommandResult(
        program=execution.cmd.program,
        argv=execution.cmd.argv,
        exit_code=exit_code,
        pid=run.process.pid if run.process.pid is not None else -1,
        stdout=stdout_text,
        stderr=stderr_text,
    )
