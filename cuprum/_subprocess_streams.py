"""Wire a spawned subprocess's stdout and stderr to their destinations.

The wiring half of single-command stream handling: this module decides *where*
each mirrored stream goes — a live presentation-sink session's log writer, the
execution context's configured sink, or the parent process's own stream — and
spawns the consumer tasks that drain the child's pipes into those destinations.
``cuprum._streams`` owns the consumption machinery those tasks run; this module
only picks the destination and starts them. ``cuprum._subprocess_stream_run``
drives the run — waiting for exit, reconciling its tasks, and settling the
per-stream relay diagnostics — and is what consumes these helpers, which the
execution module re-exports for the runs it drives.

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

    This is the middle rung of one resolution the whole run shares: the
    sibling :meth:`cuprum._sink_lifecycle._SinkBracket.resolve_destination`
    answers the session half for destinations that have no caller-configured
    sink between them, and the pipeline config composes that half with the
    caller's choice made here.

    Returns
    -------
    typ.IO[str]
        The destination for this stream's echoed output.
    """
    if session is not None:
        return session.log
    return fallback if configured is None else configured


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
    stderr_sink = _resolve_stream_sink(
        execution.sink_session, execution.ctx.stderr_sink, sys.stderr
    )
    stderr_config = dc.replace(
        stream_config,
        echo_output=execution.echo_stderr,
        sink=stderr_sink,
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
        activity=execution.idle.note_activity if execution.idle is not None else None,
    )


__all__ = [
    "_StreamConsumerSpawnContext",
    "_build_stream_config",
    "_create_stream_callback",
    "_resolve_stream_sink",
    "_spawn_stream_consumers",
]
