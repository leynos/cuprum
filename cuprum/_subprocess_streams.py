"""Stream-consumer construction for a single subprocess run.

``cuprum._subprocess_execution`` spawns the child, waits for it, and assembles
the result; this module owns the pair of stream consumers that drain its pipes
once it is running. It is split out to keep the execution module's line budget
and its concern — the run's lifecycle — intact.

The stderr config always carries the keepalive cursor, and the stdout config
picks it up only when both resolved sinks are the same object, because that is
the case where a newline-less echo can strand the diagnostic mid-line.
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


def _create_stream_callback(
    observation: _StageObservation,
    event_type: typ.Literal["stdout", "stderr"],
    pid: int | None,
) -> cabc.Callable[[str], None] | None:
    """Create a callback for emitting stream line events, or None if no hooks."""
    if not observation.hooks.observe_hooks:
        return None
    return lambda line: observation.emit(event_type, _EventDetails(pid=pid, line=line))


def _build_stream_config(
    execution: _SubprocessExecution,
    discard_on_cancel: asyncio.Event,
) -> _StreamConfig:
    """Build the stdout _StreamConfig for an execution context."""
    return _StreamConfig(
        capture_output=execution.capture,
        echo_output=execution.echo_stdout,
        echo_max_line_bytes=execution.max_echo_line_bytes,
        sink=(
            execution.ctx.stdout_sink
            if execution.ctx.stdout_sink is not None
            else sys.stdout
        ),
        encoding=execution.ctx.encoding,
        errors=execution.ctx.errors,
        discard_on_cancel=discard_on_cancel,
        read_size=_current_read_size(),
        activity=execution.idle.note_activity if execution.idle is not None else None,
    )


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
    stderr_config = dc.replace(
        stream_config,
        echo_output=execution.echo_stderr,
        sink=(
            execution.ctx.stderr_sink
            if execution.ctx.stderr_sink is not None
            else sys.stderr
        ),
        stream=EchoStream.STDERR,
        mirror=execution.idle.mirror if execution.idle is not None else None,
    )
    # The cursor tracks where the keepalive's destination ended up, not which
    # stream wrote there: a caller may point both sinks at one object, and then
    # a newline-less stdout echo strands the diagnostic exactly as a stderr one
    # would. Resolved sinks are compared, because that is where the bytes land.
    if stream_config.sink is stderr_config.sink:
        stream_config = dc.replace(stream_config, mirror=stderr_config.mirror)
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


__all__ = [
    "_build_stream_config",
    "_create_stream_callback",
    "_spawn_stream_consumers",
]
