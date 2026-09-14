"""Wire a spawned subprocess's stdout and stderr to their destinations.

The wiring half of single-command stream handling: this module decides *where*
each mirrored stream goes — a live presentation-sink session's log writer, the
execution context's configured sink, or the parent process's own stream — and
spawns the consumer tasks that drain the child's pipes into those destinations.
``cuprum._streams`` owns the consumption machinery those tasks run; this module
only picks the destination and starts them. Consumed by
``cuprum._subprocess_execution``, which re-exports the wiring helpers for the
runs it drives.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import sys
import typing as typ

from cuprum._pipeline_types import _EventDetails, _StageObservation
from cuprum._streams import _consume_stream, _StreamConfig
from cuprum._streams_pump import _current_read_size
from cuprum.echo_events import EchoStream

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.sinks.base import OutputSession


def _resolve_stream_sink(
    session: OutputSession | None,
    configured: typ.IO[str] | None,
    fallback: typ.IO[str],
) -> typ.IO[str]:
    """Return the sink echoed output for one stream is written to.

    A live presentation-sink session owns the destination, so mirrored output
    lands inside the adapter's framing — the GitHub Actions group, say — in the
    order the adapter received it. Without a session the caller-configured sink
    wins, and the process's own stream is the last resort.

    Returns
    -------
    typ.IO[str]
        The destination for this stream's echoed output.
    """
    if session is not None:
        return session.log
    return fallback if configured is None else configured


def _create_stream_callback(
    observation: _StageObservation,
    event_type: typ.Literal["stdout", "stderr"],
    pid: int | None,
) -> cabc.Callable[[str], None] | None:
    """Create a callback for emitting stream line events, or None if no hooks."""
    if not observation.hooks.observe_hooks:
        return None
    return lambda line: observation.emit(event_type, _EventDetails(pid=pid, line=line))


def _spawn_stream_consumers(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    stream_config: _StreamConfig,
    *,
    pid: int | None,
) -> tuple[asyncio.Task[str | None], asyncio.Task[str | None]]:
    """Spawn stdout and stderr stream consumer tasks."""
    stdout_on_line = _create_stream_callback(execution.observation, "stdout", pid)
    stderr_on_line = _create_stream_callback(execution.observation, "stderr", pid)
    stderr_sink = _resolve_stream_sink(
        execution.sink_session, execution.ctx.stderr_sink, sys.stderr
    )
    stderr_config = dc.replace(
        stream_config,
        echo_output=execution.echo_stderr,
        sink=stderr_sink,
        stream=EchoStream.STDERR,
    )
    return (
        asyncio.create_task(
            _consume_stream(
                process.stdout,
                stream_config,
                on_line=stdout_on_line,
                read_size=stream_config.read_size,
            ),
        ),
        asyncio.create_task(
            _consume_stream(
                process.stderr,
                stderr_config,
                on_line=stderr_on_line,
                read_size=stderr_config.read_size,
            ),
        ),
    )


def _build_stream_config(
    execution: _SubprocessExecution,
    discard_on_cancel: asyncio.Event,
) -> _StreamConfig:
    """Build the stdout _StreamConfig for an execution context.

    When a presentation-sink session is active, mirrored stdout is routed
    through the session's log destination so it lands inside the adapter's
    framing (for example, inside the GitHub Actions group) in the order the
    adapter received it.

    Returns
    -------
    _StreamConfig
        The stream configuration for the run's stdout drain.
    """
    stdout_sink = _resolve_stream_sink(
        execution.sink_session, execution.ctx.stdout_sink, sys.stdout
    )
    return _StreamConfig(
        capture_output=execution.capture,
        echo_output=execution.echo_stdout,
        echo_max_line_bytes=execution.max_echo_line_bytes,
        sink=stdout_sink,
        encoding=execution.ctx.encoding,
        errors=execution.ctx.errors,
        discard_on_cancel=discard_on_cancel,
        read_size=_current_read_size(),
    )


__all__ = [
    "_build_stream_config",
    "_create_stream_callback",
    "_resolve_stream_sink",
    "_spawn_stream_consumers",
]
