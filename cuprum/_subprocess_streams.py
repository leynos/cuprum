"""Stream-consumer construction for a single subprocess run.

``cuprum._subprocess_stream_run`` drives the run — waiting for exit,
reconciling its tasks, and settling the per-stream relay diagnostics — while
this module owns the pair of stream consumers that drain the child's pipes once
it is running, plus the configuration they drain with. It is split out to keep
the execution module's line budget and its concern — the run's lifecycle —
intact.

The stderr config always carries the keepalive cursor, and the stdout config
picks it up only when both resolved sinks are the same object, because that is
the case where a newline-less echo can strand the diagnostic mid-line.

The per-line callback for each stream is composed here — the observe-hook
emission plus the caller's ``on_line``, both via
``cuprum._line_callbacks._compose_line_callbacks`` — so ``SafeCmd.run()`` and
``SafeCmd.lines()`` share one composition seam. Each consumer drains into the
collector the run built for it: index ``0`` is stdout's, index ``1`` is
stderr's. The spawn helper does not own those collectors; the run retains the
same tuple so its single reconciliation point settles and reads them exactly
once.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import sys
import typing as typ

from cuprum._line_callbacks import _compose_line_callbacks, _LineEmissionContext
from cuprum._streams import _consume_stream, _RelayDiagnostics, _StreamConfig
from cuprum._streams_pump import _current_read_size
from cuprum.echo_events import EchoStream

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.lines import LineStreamName, _LineHookOutcome


def _create_stream_callback(
    execution: _SubprocessExecution,
    event_type: LineStreamName,
    pid: int | None,
) -> cabc.Callable[[str], _LineHookOutcome] | None:
    """Create the composed per-line callback for one stream, or ``None``."""
    emission = _LineEmissionContext(
        stream="stdout",
        pid=pid,
        on_line=execution.on_line,
        started_at=execution.started_at,
    )
    return _compose_line_callbacks(
        execution.observation,
        dc.replace(emission, stream=event_type),
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
        read_size=_current_read_size(),
        activity=execution.idle.note_activity if execution.idle is not None else None,
    )


@dc.dataclass(frozen=True, slots=True)
class _StreamConsumerSpawnContext:
    """Inputs a run hands to its stream-consumer spawn.

    Bundles the stdout stream configuration, the subprocess PID, and the
    per-stream relay diagnostics collectors so the spawn helper takes one
    argument instead of three. The context is created before any consumer
    task exists; ownership of the collectors stays with the run, which
    continues to retain the same tuple on its ``_RunTaskOwnership``.
    """

    stream_config: _StreamConfig
    pid: int | None
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics]


def _spawn_stream_consumers(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    spawn_context: _StreamConsumerSpawnContext,
) -> tuple[asyncio.Task[str | None], asyncio.Task[str | None]]:
    """Spawn stdout and stderr stream consumer tasks.

    Each consumer drains into its collector from ``spawn_context``:
    index ``0`` is stdout's, index ``1`` is stderr's. The caller retains the
    pair on its ``_RunTaskOwnership`` so its single reconciliation point can
    settle and read them exactly once.

    Returns
    -------
    tuple[asyncio.Task[str | None], asyncio.Task[str | None]]
        The stdout and stderr consumer tasks, in that order.
    """
    pid = spawn_context.pid
    stream_config = spawn_context.stream_config
    relay_diagnostics = spawn_context.relay_diagnostics
    stdout_on_line = _create_stream_callback(execution, "stdout", pid)
    stderr_on_line = _create_stream_callback(execution, "stderr", pid)
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
                relay_diagnostics=relay_diagnostics[0],
            ),
        ),
        asyncio.create_task(
            _consume_stream(
                process.stderr,
                stderr_config,
                on_line=stderr_on_line,
                relay_diagnostics=relay_diagnostics[1],
            ),
        ),
    )


__all__ = [
    "_StreamConsumerSpawnContext",
    "_build_stream_config",
    "_create_stream_callback",
    "_spawn_stream_consumers",
]
