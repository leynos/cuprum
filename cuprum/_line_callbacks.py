"""Composition of per-line callbacks for subprocess output streams.

Both the single-command path (``cuprum._subprocess_execution``) and the
pipeline path (``cuprum._pipeline_stage_streams``) turn one decoded line into
the same set of fan-outs: the observe-hook emission that already exists, and
the caller's ``on_line`` callback with a stamped ``LineEvent``. Owning that
composition here keeps the two paths from growing divergent closures and keeps
the stamping seam — a reading of the run's monotonic clock relative to its start
reference — in one place.

When nothing observes lines (no observe hooks, no ``on_line``), composition
returns ``None`` so the zero-callback drain path keeps its current cost.

The composed callback returns whatever ``on_line`` returned, so a hook that
answers with an awaitable holds the read loop: that is what lets the
``lines()`` driver's bounded queue push back on a chatty child. Observe-hook
work is bounded the same way: once the run retains
:data:`cuprum._observe_task_retention._OBSERVE_TASK_CEILING` unfinished observer
tasks, the next line's observation waits for one of them to finish, which holds
the read loop and, through it, the child's pipe.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum._line_event_emitter import _LineEventEmitter
from cuprum.lines import (
    LineEvent,
    LineStreamName,
    _LineHookFn,
    _LineHookOutcome,
    perf_counter,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import _StageObservation


@dc.dataclass(frozen=True, slots=True)
class _LineEmissionContext:
    """Per-stream fan-out context shared by both consumption paths.

    Attributes
    ----------
    stream:
        Which output stream the lines arrive on.
    pid:
        Subprocess identifier attached to observe events; ``None`` before
        spawn.
    on_line:
        The caller's line callback, when one is registered.
    started_at:
        Monotonic reference the ``at`` stamps are measured from.
    monotonic_clock:
        The clock the stamps are read from. It must be the same callable
        ``started_at`` was sampled from, and the same one the post-exit
        deadline subtracts it from, or the difference is not a duration.
        Production paths pass the run's observation clock; the default keeps
        the historical seam for constructions that supply none.

    """

    stream: LineStreamName
    pid: int | None
    on_line: _LineHookFn | None
    started_at: float
    monotonic_clock: cabc.Callable[[], float] = perf_counter


@dc.dataclass(frozen=True, slots=True)
class _StageStreamCallbacks:
    """The per-stream values a caller supplies; the clock is not one of them.

    Attributes
    ----------
    pid:
        Subprocess identifier attached to observe events; ``None`` before
        spawn.
    on_line:
        The caller's line callback, when one is registered.
    started_at:
        Monotonic reference the ``at`` stamps are measured from. It must be a
        reading of the observation's own clock, which is why the composing
        helper takes the observation rather than a clock alongside it.

    """

    pid: int | None
    on_line: _LineHookFn | None
    started_at: float


def _stamp_line(line: str, *, context: _LineEmissionContext) -> LineEvent:
    """Build a ``LineEvent`` stamped with the elapsed monotonic time."""
    return LineEvent(
        stream=context.stream,
        at=max(0.0, context.monotonic_clock() - context.started_at),
        text=line,
    )


def _line_event_emitter(
    observation: _StageObservation,
    context: _LineEmissionContext,
) -> _LineEventEmitter | None:
    """Bind one stream's invariant event fields, or ``None`` to skip emitting.

    Returns
    -------
    _LineEventEmitter | None
        A prepared emitter, or ``None`` when no observe hook is installed, so
        a stream nobody observes pays nothing to prepare.

    Notes
    -----
    The emitter holds ``observation._emit_event`` rather than a copy of its
    body. That method catches ``_ExecEventEmissionError``, retains the
    observation's pending-task list, and owns the tasks of hooks that already
    ran when a later hook failed, so re-implementing the dispatch here — or
    reaching past it to ``_emit_exec_event`` — would silently drop that
    ownership. The private access is deliberate and confined to this factory.

    """
    if not observation.hooks.observe_hooks:
        return None
    return _LineEventEmitter(
        program=observation.cmd.program,
        argv=observation.cmd.argv_with_program,
        cwd=observation.cwd,
        env=observation.env_overlay,
        env_mode=observation.env_mode,
        resolved_path=observation.resolved_path,
        capture_mode=observation.capture_mode,
        pid=context.pid,
        stream=context.stream,
        tags=observation.tags,
        project=observation.cmd.project.name,
        exec_id=observation.exec_id,
        wall_clock=observation.wall_clock,
        emit_event=observation._emit_event,
        pending_tasks=observation.pending_tasks,
        telemetry=observation.retention_telemetry,
    )


def _compose_line_callbacks(
    observation: _StageObservation,
    context: _LineEmissionContext,
) -> cabc.Callable[[str], _LineHookOutcome] | None:
    """Compose the observe-hook emission and the user ``on_line`` per line.

    Three shapes are possible. With nothing observing the stream the result is
    ``None``. With observation capacity available the callback emits and calls
    ``on_line`` inline, returning the hook's outcome unchanged so the caller
    still owns the backpressure decision. At the retention ceiling it returns
    a coroutine instead: that coroutine waits for a slot, emits, and then
    calls ``on_line``, preserving the observation-before-callback order while
    holding the read loop for as long as the wait lasts.

    Only the last shape suspends. Keeping the other two synchronous means a
    line whose observation fits costs exactly what it cost before, and the
    caller's hook is never delayed behind an unrelated scheduling turn.

    Returns
    -------
    collections.abc.Callable[[str], _LineHookOutcome] | None
        A callback invoked once per decoded line, or ``None`` when neither the
        observe hooks nor a user callback needs the stream, preserving the
        zero-cost no-line-observer path. Its return value is whatever
        ``on_line`` returned, so a hook that applies backpressure is passed
        through to the caller's ``await``.
    """
    emitter = _line_event_emitter(observation, context)
    if emitter is None and context.on_line is None:
        return None

    def emit_line(line: str) -> _LineHookOutcome:
        """Fan one line out, deferring observation when the bound is reached."""
        if emitter is None:
            return _invoke_on_line(line, context=context)
        if emitter.admit_observation():
            emitter.emit_line(line)
            return _invoke_on_line(line, context=context)
        # Stamp the line now, while it is still the line just decoded. The wait
        # inside the coroutine can last as long as a slow observer takes, and
        # ``LineEvent.at`` promises the decode moment, not the delivery one.
        # The wall-clock ``ExecEvent.timestamp`` is deliberately not captured
        # here: it records when the event is emitted.
        stamped = (
            None if context.on_line is None else _stamp_line(line, context=context)
        )
        return _emit_line_when_capacity(
            emitter,
            line,
            context=context,
            event=stamped,
        )

    return emit_line


def _invoke_on_line(
    line: str,
    *,
    context: _LineEmissionContext,
    event: LineEvent | None = None,
) -> _LineHookOutcome:
    """Stamp ``line`` — or reuse a deferred stamp — for the caller's hook."""
    if context.on_line is None:
        return None
    return context.on_line(
        _stamp_line(line, context=context) if event is None else event
    )


async def _emit_line_when_capacity(
    emitter: _LineEventEmitter,
    line: str,
    *,
    context: _LineEmissionContext,
    event: LineEvent | None = None,
) -> None:
    """Wait for observer capacity, then emit ``line`` and call ``on_line``.

    Emitting inside the coroutine means a synchronous observation failure
    reaches the awaiting drain loop as a raised exception before ``on_line``
    runs, so a callback never observes a line whose event was not dispatched.
    The wait itself is cancelled without disturbing the observer tasks it was
    waiting on; they belong to the run and are settled by its final drain.

    ``event`` carries the ``LineEvent`` stamped before the wait, so a deferred
    line reports the moment it was decoded rather than the moment a slot freed.
    """
    await emitter.await_capacity()
    emitter.emit_line(line)
    outcome = _invoke_on_line(line, context=context, event=event)
    if outcome is not None:
        await outcome


def _compose_observation_line_callback(
    observation: _StageObservation,
    stream: LineStreamName,
    context: _StageStreamCallbacks,
) -> cabc.Callable[[str], _LineHookOutcome] | None:
    """Compose one stream's callbacks, taking its clock from the observation.

    Every production path samples its start reference from the observation's
    ``monotonic_clock`` and subtracts it when stamping, and the post-exit
    deadline is read from the same callable. Building the emission context
    here is what keeps that one-clock rule true by construction: a caller
    supplies the start reference and nothing else, so it cannot pair that
    reference with a different clock.

    Returns
    -------
    collections.abc.Callable[[str], _LineHookOutcome] | None
        The composed callback, or ``None`` when nothing observes this stream.
    """
    return _compose_line_callbacks(
        observation,
        _LineEmissionContext(
            stream=stream,
            pid=context.pid,
            on_line=context.on_line,
            started_at=context.started_at,
            monotonic_clock=observation.monotonic_clock,
        ),
    )
