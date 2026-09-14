"""Stream-consumer wiring for a spawned subprocess.

``cuprum._subprocess_execution`` spawns the process and coordinates the run;
this module owns what reads its pipes and which callbacks see the lines. Both
entry points reach the consumers here — ``SafeCmd.run()`` through the execution
module, and ``SafeCmd.lines()`` through ``cuprum._line_stream`` — so the
per-line callback composition is wired in exactly one place. See ADR 007 for
why the subprocess machinery is divided this way.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import sys
import typing as typ

from cuprum._line_callbacks import _compose_line_callbacks, _LineEmissionContext
from cuprum._streams import _consume_stream, _StreamConfig
from cuprum.echo_events import EchoStream

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import _StageObservation
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.lines import LineStreamName, _LineHookOutcome


def _create_stream_callback(
    observation: _StageObservation,
    event_type: LineStreamName,
    emission: _LineEmissionContext,
) -> cabc.Callable[[str], _LineHookOutcome] | None:
    """Create the composed per-line callback for one stream, or ``None``."""
    return _compose_line_callbacks(
        observation,
        dc.replace(emission, stream=event_type),
    )


def _spawn_stream_consumers(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    stream_config: _StreamConfig,
    *,
    pid: int | None,
) -> tuple[asyncio.Task[str | None], asyncio.Task[str | None]]:
    """Spawn stdout and stderr stream consumer tasks."""
    emission = _LineEmissionContext(
        stream="stdout",
        pid=pid,
        on_line=execution.on_line,
        started_at=execution.started_at,
    )
    stdout_on_line = _create_stream_callback(
        execution.observation,
        "stdout",
        emission,
    )
    stderr_on_line = _create_stream_callback(
        execution.observation,
        "stderr",
        emission,
    )
    stderr_config = dc.replace(
        stream_config,
        echo_output=execution.echo_stderr,
        sink=(
            execution.ctx.stderr_sink
            if execution.ctx.stderr_sink is not None
            else sys.stderr
        ),
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


__all__ = ["_create_stream_callback", "_spawn_stream_consumers"]
