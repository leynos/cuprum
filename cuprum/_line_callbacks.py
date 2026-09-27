"""Composition of per-line callbacks for subprocess output streams.

Both the single-command path (``cuprum._subprocess_execution``) and the
pipeline path (``cuprum._pipeline_stage_streams``) turn one decoded line into
the same set of fan-outs: the observe-hook emission that already exists, and
the caller's ``on_line`` callback with a stamped ``LineEvent``. Owning that
composition here keeps the two paths from growing divergent closures and keeps
the stamping seam — the ``perf_counter`` clock read relative to a start
reference — in one place.

When nothing observes lines (no observe hooks, no ``on_line``), composition
returns ``None`` so the zero-callback drain path keeps its current cost.

The composed callback returns whatever ``on_line`` returned, so a hook that
answers with an awaitable holds the read loop: that is what lets the
``lines()`` driver's bounded queue push back on a chatty child.
"""

from __future__ import annotations

import dataclasses as dc
import functools
import inspect
import typing as typ

from cuprum.events import ExecEvent
from cuprum.lines import (
    LineEvent,
    LineStreamName,
    _LineHookFn,
    _LineHookOutcome,
    perf_counter,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum._pipeline_types import _StageObservation
    from cuprum.events import ExecId
    from cuprum.program import Program


def _stamp_line(
    line: str,
    *,
    stream: LineStreamName,
    started_at: float,
) -> LineEvent:
    """Build a ``LineEvent`` stamped with the elapsed monotonic time."""
    return LineEvent(
        stream=stream,
        at=max(0.0, perf_counter() - started_at),
        text=line,
    )


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

    """

    stream: LineStreamName
    pid: int | None
    on_line: _LineHookFn | None
    started_at: float


@dc.dataclass(frozen=True, slots=True)
class _LineEventEmitter:
    """The invariant half of a line ``ExecEvent``, bound once per stream.

    Every field here is fixed for the life of one observed stream, so a line
    event needs only a line and a timestamp to complete it. ``emit`` rebuilds
    all of them — including ``argv_with_program``, which walks the whole argv
    tuple — for every line, which is the cost this type exists to remove.

    Attributes
    ----------
    program:
        The allowlisted program, read from the command once.
    argv:
        The full argv with the program name first, resolved once.
    cwd:
        Working directory for the execution, when set.
    env:
        Environment overlay for the execution, when set.
    pid:
        Subprocess identifier, ``None`` before spawn.
    stream:
        Which output stream the lines arrive on; this doubles as the event
        phase, so no ``Literal["stdout", "stderr"]`` alias is needed.
    tags:
        The observation's tag mapping, shared not copied.
    project:
        Trusted project name, read from the command once.
    exec_id:
        Correlation token minted once per stage observation.
    wall_clock:
        The clock read once per emitted line.
    emit_event:
        The observation's own dispatcher, bound rather than called, so the
        emission still runs through ``_emit_event``.

    """

    program: Program
    argv: tuple[str, ...]
    cwd: Path | None
    env: cabc.Mapping[str, str] | None
    pid: int | None
    stream: LineStreamName
    tags: cabc.Mapping[str, object]
    project: str
    exec_id: ExecId
    wall_clock: cabc.Callable[[], float]
    emit_event: cabc.Callable[[ExecEvent], None]

    def emit_line(self, line: str) -> None:
        """Emit one line event from the values bound at preparation time."""
        self.emit_event(
            ExecEvent(
                phase=self.stream,
                program=self.program,
                argv=self.argv,
                cwd=self.cwd,
                env=self.env,
                pid=self.pid,
                timestamp=self.wall_clock(),
                line=line,
                exit_code=None,
                duration_s=None,
                tags=self.tags,
                project=self.project,
                exec_id=self.exec_id,
            ),
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
        pid=context.pid,
        stream=context.stream,
        tags=observation.tags,
        project=observation.cmd.project.name,
        exec_id=observation.exec_id,
        wall_clock=observation.wall_clock,
        emit_event=observation._emit_event,
    )


def _compose_line_callbacks(
    observation: _StageObservation,
    context: _LineEmissionContext,
) -> cabc.Callable[[str], _LineHookOutcome] | None:
    """Compose the observe-hook emission and the user ``on_line`` per line.

    Returns
    -------
    collections.abc.Callable[[str], _LineHookOutcome] | None
        A callback invoked once per decoded line, or ``None`` when neither the
        observe hooks nor a user callback needs the stream, preserving the
        zero-cost no-line-observer path. Its return value is whatever
        ``on_line`` returned, so a hook that applies backpressure is passed
        through to the caller's ``await``.
    """
    has_observe_hooks = bool(observation.hooks.observe_hooks)
    if not has_observe_hooks and context.on_line is None:
        return None

    emitter = _line_event_emitter(observation, context)

    def emit_line(line: str) -> _LineHookOutcome:
        """Fan one line out, returning the hook's awaitable when it has one."""
        if emitter is not None:
            emitter.emit_line(line)
        if context.on_line is None:
            return None
        return context.on_line(
            _stamp_line(
                line,
                stream=context.stream,
                started_at=context.started_at,
            ),
        )

    return emit_line


def _chain_line_hooks(
    hooks: cabc.Iterable[_LineHookFn | None],
) -> _LineHookFn | None:
    """Chain several line hooks into one, in the order supplied.

    ``SafeCmd.lines()`` registers the caller's ``on_line`` alongside the
    driver's queue sink, and both must see every line: the caller's hook runs
    first so its view of the stream never depends on how the driver delivers.

    Returns
    -------
    _LineHookFn | None
        ``None`` when nothing was supplied, the sole hook when exactly one was,
        and a fan-out over all of them otherwise. The fan-out collects every
        hook's awaitable and returns one awaitable of its own, so the drain
        loop still waits for each before reading on.
    """
    chain = [hook for hook in hooks if hook is not None]
    if not chain:
        return None
    if len(chain) == 1:
        return chain[0]
    return _fan_out_hooks(chain)


def _fan_out_hooks(chain: cabc.Sequence[_LineHookFn]) -> _LineHookFn:
    """Return one hook that delivers every event to each hook in *chain*."""
    return typ.cast("_LineHookFn", functools.partial(_fan_out_event, tuple(chain)))


def _fan_out_event(
    chain: cabc.Sequence[_LineHookFn],
    event: LineEvent,
) -> _LineHookOutcome:
    """Deliver one event to every hook before awaiting their outcomes."""
    pending = [outcome for hook in chain if (outcome := hook(event)) is not None]
    if not pending:
        return None
    return _await_hook_outcomes(pending)


async def _await_hook_outcomes(pending: cabc.Sequence[cabc.Awaitable[None]]) -> None:
    """Await every deferred hook, in registration order."""
    for index, outcome in enumerate(pending):
        try:
            await outcome
        except BaseException:
            _close_skipped_hook_outcomes(pending[index + 1 :])
            raise


def _close_skipped_hook_outcomes(pending: cabc.Iterable[cabc.Awaitable[None]]) -> None:
    """Close unawaited coroutines left after an earlier hook failure."""
    for outcome in pending:
        if inspect.iscoroutine(outcome):
            outcome.close()
