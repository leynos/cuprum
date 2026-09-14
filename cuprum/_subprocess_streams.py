"""Wiring a single command's stdout and stderr for the parent.

The single-command counterpart of ``cuprum._pipeline_stage_streams``: given the
spawned process and the options the caller chose, decide what each stream's
:class:`~cuprum._streams._StreamConfig` is and start the consumers that drain
them. Line observers are attached here too, so a stream the parent does not
consume still reports its lines to the observe hooks.

The names are re-exported from ``cuprum._subprocess_execution``, which is where
callers and tests have always imported them from.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import sys
import typing as typ

from cuprum._pipeline_types import _EventDetails
from cuprum._streams import _consume_stream, _StreamConfig
from cuprum.echo_events import EchoStream

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import _StageObservation
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
        # Only the stderr echo shares a destination with the keepalive, so it
        # alone can leave a line for the diagnostic to trip over.
        mirror=execution.idle.mirror if execution.idle is not None else None,
    )
    return (
        asyncio.create_task(
            _consume_stream(
                process.stdout,
                stream_config,
                on_line=stdout_on_line,
            ),
        ),
        asyncio.create_task(
            _consume_stream(
                process.stderr,
                stderr_config,
                on_line=stderr_on_line,
            ),
        ),
    )


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
        activity=execution.idle.note_activity if execution.idle is not None else None,
    )


__all__ = [
    "_build_stream_config",
    "_create_stream_callback",
    "_spawn_stream_consumers",
]
