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

``_prepare_stream_spawn`` is the shared entry point for that wiring. The direct
run, the line iterator, and the owned handle all wire their consumers through
it, because each documents the same requirement — that its consumers run
exactly the configuration the others run — and three copies of that
construction is how the requirement stops being true.

The stderr config always carries the keepalive cursor, and the stdout config
picks it up only when both resolved sinks are the same object, because that is
the case where a newline-less echo can strand the diagnostic mid-line.

The per-line callback for each stream is composed here — the observe-hook
emission plus the caller's ``on_line``, both via
``cuprum._line_callbacks._compose_observation_line_callback`` — so
``SafeCmd.run()`` and ``SafeCmd.lines()`` share one composition seam, and the
clock every stamp is measured against is bound by that seam rather than by each
caller. Each consumer drains into the collector the run built for it: index
``0`` is stdout's, index ``1`` is stderr's. The spawn helper does not own those
collectors; the run retains the same tuple so its single reconciliation point
settles and reads them exactly once.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import sys
import typing as typ

from cuprum._line_callbacks import (
    _compose_observation_line_callback,
    _StageStreamCallbacks,
)
from cuprum._stream_config import (
    # Re-exported under this module's names as well as imported for use: both
    # once lived here, and ``_command_internals`` still reaches the sink
    # resolver by this path. ``__all__`` below keeps that path open.
    _resolve_stream_configs as _resolve_stream_configs,
)
from cuprum._stream_config import (
    _resolve_stream_sink as _resolve_stream_sink,
)
from cuprum._streams import _consume_stream, _RelayDiagnostics, _StreamConfig
from cuprum._streams_pump import _current_read_size
from cuprum._subprocess_stdin import _spawn_stdin_writer
from cuprum._subprocess_stdin_stream import _stdin_codec
from cuprum._subprocess_wait_types import _RunTaskOwnership

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._capture_types import CaptureSink
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_wait_types import _ConsumerPair, _StreamConsumerTask
    from cuprum.lines import LineStreamName, _LineHookOutcome


def _create_stream_callback(
    execution: _SubprocessExecution,
    event_type: LineStreamName,
    pid: int | None,
) -> cabc.Callable[[str], _LineHookOutcome] | None:
    """Create the composed per-line callback for one stream, or ``None``.

    Composed through the observation-taking helper, which binds the stamp's
    clock itself. Building the emission context here would let this path pair
    ``execution.started_at`` with a clock other than the one it was sampled
    from, and every stamp would then be a difference between two clocks.

    Returns
    -------
    collections.abc.Callable[[str], _LineHookOutcome] | None
        The composed callback, or ``None`` when nothing observes this stream.
    """
    return _compose_observation_line_callback(
        execution.observation,
        event_type,
        _StageStreamCallbacks(
            pid=pid,
            on_line=execution.on_line,
            started_at=execution.started_at,
        ),
    )


@dc.dataclass(frozen=True, slots=True)
class _StreamConsumerSpawnContext:
    """Inputs a run hands to its stream-consumer spawn.

    Bundles the *resolved* per-stream configurations, the subprocess PID, and
    the per-stream relay diagnostics collectors so the spawn helper takes one
    argument instead of three. The context is created before any consumer task
    exists; ownership of the collectors stays with the run, which continues to
    retain the same tuple on its ``_RunTaskOwnership``.

    The configurations are resolved here rather than inside the spawn so that
    the run holds the very objects its consumers drain. That is what makes the
    capture accumulators reachable from teardown: a config carries the
    accumulator for its own policy, and a run that rebuilt the configs would
    hold accumulators nobody drained into while the ones holding a spill file
    went out of reach.
    """

    stdout_config: _StreamConfig
    stderr_config: _StreamConfig
    pid: int | None
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics]

    @property
    def captures(self) -> tuple[CaptureSink | None, CaptureSink | None]:
        """Report both streams' accumulators, stdout first.

        The order matches ``relay_diagnostics`` and the consumer pair, so a
        teardown indexed by stream reaches the right accumulator for whichever
        stream it is cleaning up.
        """
        return (self.stdout_config.capture_sink, self.stderr_config.capture_sink)


@dc.dataclass(frozen=True, slots=True)
class _StreamSpawnInputs:
    """Everything a streamed run needs before it starts its consumers.

    The three runs that own a child's streams — the direct run, the line
    iterator, and the owned handle — build the same configuration, the same
    diagnostics tuple, and the same spawn context. Bundling them here keeps
    that construction in one place, because each caller documents the same
    invariant: the consumers it starts must be wired exactly as the others
    wire theirs, or the paths silently diverge on capture, echo, and sink
    selection.

    ``discard_on_cancel`` and ``relay_diagnostics`` are carried alongside the
    context because the run retains both on its ``_RunTaskOwnership``: the
    context is a value snapshot for the spawn, not a hand-off of ownership.
    """

    discard_on_cancel: asyncio.Event
    spawn_context: _StreamConsumerSpawnContext
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics]


def _build_run_task_ownership(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    spawn: _StreamSpawnInputs,
) -> _RunTaskOwnership:
    """Start a run's stdin writer and stream consumers as one owned set.

    The owned-handle path builds its equivalent inline rather than calling
    this, because attaching there is all-or-nothing: it has to record which
    tasks it started so it can reclaim them when a later start fails. The two
    paths that have nothing to reclaim — the direct run and the line iterator
    — both come through here.

    Returns
    -------
    _RunTaskOwnership
        The started tasks, plus the latch and collectors the run retains.
    """
    return _RunTaskOwnership(
        stdin_task=_spawn_stdin_writer(
            process,
            execution.stdio.stdin,
            _stdin_codec(execution.ctx),
            execution.observation,
        ),
        consumers=_spawn_stream_consumers(process, execution, spawn.spawn_context),
        discard_on_cancel=spawn.discard_on_cancel,
        relay_diagnostics=spawn.relay_diagnostics,
        idle=execution.idle,
        captures=spawn.spawn_context.captures,
    )


def _spawn_stream_consumers(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    spawn_context: _StreamConsumerSpawnContext,
) -> _ConsumerPair:
    """Spawn a consumer for each stream cuprum holds a pipe for.

    Each consumer drains into its collector from ``spawn_context``:
    index ``0`` is stdout's, index ``1`` is stderr's. The caller retains the
    pair on its ``_RunTaskOwnership`` so its single reconciliation point can
    settle and read them exactly once.

    The gate is whether cuprum owns a pipe, not whether anything reads it. A
    stream that is piped but unread still gets its consumer, exactly as it did
    before redirectable streams existed: the task settles immediately against a
    ``None`` reader and reports the empty capture the run's result already
    carries. A stream bound to a file, a borrowed descriptor, or the parent's
    own stream is a different matter — attaching a ``StreamReader`` to a
    descriptor cuprum does not own is the one thing this must never do, so
    those streams get no task at all and a ``None`` in their slot.

    Returns
    -------
    _ConsumerPair
        The stdout and stderr consumer tasks, in that order. A slot is
        ``None`` for a stream cuprum does not pipe.
    """
    relay_diagnostics = spawn_context.relay_diagnostics
    stdout_on_line = _create_stream_callback(execution, "stdout", spawn_context.pid)
    stderr_on_line = _create_stream_callback(execution, "stderr", spawn_context.pid)
    return (
        _spawn_consumer(
            _ConsumerWiring(
                stream=process.stdout,
                is_pipe=execution.stdio.stdout.is_pipe,
                config=spawn_context.stdout_config,
                on_line=stdout_on_line,
                relay_diagnostics=relay_diagnostics[0],
            )
        ),
        _spawn_consumer(
            _ConsumerWiring(
                stream=process.stderr,
                is_pipe=execution.stdio.stderr.is_pipe,
                config=spawn_context.stderr_config,
                on_line=stderr_on_line,
                relay_diagnostics=relay_diagnostics[1],
            )
        ),
    )


@dc.dataclass(frozen=True, slots=True)
class _ConsumerWiring:
    """Everything one stream's consumer needs to be constructed.

    Bundled rather than passed as five arguments, and bundled *here* rather
    than in the spawn helper's signature for a reason the ``is_pipe`` field
    makes concrete: whether cuprum owns a pipe is a property of the stream
    being wired, so it belongs beside that stream's reader and that stream's
    config. A bare boolean in a positional list would be a trap; a field named
    ``is_pipe`` on the record it describes is not.

    The values are read from the execution and the spawn context by
    :func:`_spawn_stream_consumers`, which is the only place that knows which
    stream is being wired, so a caller cannot pair a reader with a config
    belonging to the other stream.
    """

    stream: asyncio.StreamReader | None
    is_pipe: bool
    config: _StreamConfig
    on_line: cabc.Callable[[str], _LineHookOutcome] | None
    relay_diagnostics: _RelayDiagnostics


def _spawn_consumer(wiring: _ConsumerWiring) -> _StreamConsumerTask | None:
    """Spawn one stream's consumer, or return ``None`` when there is no pipe.

    Parameters
    ----------
    wiring : _ConsumerWiring
        The reader, pipe flag, drain config, per-line callback, and
        echo-disablement collector for this one stream.

    Returns
    -------
    _StreamConsumerTask | None
        The consumer task, or ``None`` for a stream cuprum does not pipe, since
        the wiring named no library-owned pipe to attach a reader to.
    """
    if not wiring.is_pipe:
        return None
    return asyncio.create_task(
        _consume_stream(
            wiring.stream,
            wiring.config,
            on_line=wiring.on_line,
            relay_diagnostics=wiring.relay_diagnostics,
        ),
    )


def _build_stream_config(
    execution: _SubprocessExecution,
    discard_on_cancel: asyncio.Event,
    settlement_started: asyncio.Event | None = None,
) -> _StreamConfig:
    """Build the stdout ``_StreamConfig`` for an execution context.

    When a presentation-sink session is active, mirrored stdout is routed
    through the session's log destination so it lands inside the adapter's
    framing (for example, inside the GitHub Actions group) in the order the
    adapter received it.

    The mode and the broken-pipe policy are both set on the stdout config and
    inherited by the stderr one, which this module derives with
    ``dc.replace``: each is a property of the run rather than of a stream, so
    one value governs both, exactly as the caller specified it on the options
    object. Capture is the exception the derivation overrides: it is configured
    per stream, so the stderr policy is applied rather than inherited.

    Returns
    -------
    _StreamConfig
        The stream configuration for the run's stdout drain.
    """
    stdout_sink = _resolve_stream_sink(
        execution.sink_session, execution.ctx.stdout_sink, sys.stdout
    )
    return _StreamConfig(
        capture=execution.capture_stdout,
        echo_output=execution.echo_stdout,
        echo_max_line_bytes=execution.max_echo_line_bytes,
        max_line_chars=execution.max_line_chars,
        capture_bytes=execution.capture_bytes,
        broken_pipe_policy=execution.broken_pipe_policy,
        sink=stdout_sink,
        delivery=execution.delivery_worker,
        encoding=execution.ctx.encoding,
        errors=execution.ctx.errors,
        discard_on_cancel=discard_on_cancel,
        settlement_started=settlement_started,
        read_size=_current_read_size(),
        activity=execution.idle.note_activity if execution.idle is not None else None,
    )


def _prepare_stream_spawn(
    execution: _SubprocessExecution,
    pid: int | None,
    *,
    discard_on_cancel: asyncio.Event | None = None,
    settlement_started: asyncio.Event | None = None,
) -> _StreamSpawnInputs:
    """Build stream inputs, reusing caller latches before any consumer starts.

    The settlement event identifies line-stream teardown; an omitted discard
    latch is created here for routes that do not supply one.

    Returns
    -------
    _StreamSpawnInputs
        The latches, diagnostics collectors, and resolved stream configs for
        the consumers this run starts.
    """
    if discard_on_cancel is None:
        discard_on_cancel = asyncio.Event()
    # The run's own collectors when it made them, so bounded-delivery loss
    # reaches the same result the bounded echo path writes to; a fresh pair
    # only for the paths that never allocated any.
    relay_diagnostics = execution.relay_diagnostics
    if relay_diagnostics is None:
        relay_diagnostics = (_RelayDiagnostics(), _RelayDiagnostics())
    # Both configs are resolved before the consumers exist and the run keeps
    # the exact pair they drain, so each stream's capture accumulator stays
    # reachable from the run's single reconciliation point.
    stdout_config, stderr_config = _resolve_stream_configs(
        execution,
        _build_stream_config(execution, discard_on_cancel, settlement_started),
    )
    return _StreamSpawnInputs(
        discard_on_cancel=discard_on_cancel,
        relay_diagnostics=relay_diagnostics,
        spawn_context=_StreamConsumerSpawnContext(
            stdout_config=stdout_config,
            stderr_config=stderr_config,
            pid=pid,
            relay_diagnostics=relay_diagnostics,
        ),
    )


__all__ = [
    "_StreamConsumerSpawnContext",
    "_StreamSpawnInputs",
    "_build_run_task_ownership",
    "_build_stream_config",
    "_create_stream_callback",
    "_prepare_stream_spawn",
    "_resolve_stream_configs",
    "_resolve_stream_sink",
    "_spawn_stream_consumers",
]
