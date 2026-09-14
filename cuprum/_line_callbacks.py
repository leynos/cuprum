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
import typing as typ

from cuprum.lines import (
    LineEvent,
    LineStreamName,
    _LineHookFn,
    _LineHookOutcome,
    perf_counter,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import _EventDetails, _StageObservation


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


def _event_details(*, pid: int | None, line: str) -> _EventDetails:
    """Build one deferred-import observe-event payload."""
    from cuprum._pipeline_types import _EventDetails

    return _EventDetails(pid=pid, line=line)


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

    def emit_line(line: str) -> _LineHookOutcome:
        """Fan one line out, returning the hook's awaitable when it has one."""
        if has_observe_hooks:
            observation.emit(
                context.stream,
                _event_details(pid=context.pid, line=line),
            )
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

    async def await_all(pending: cabc.Iterable[cabc.Awaitable[None]]) -> None:
        """Await every deferred hook, in registration order."""
        for outcome in pending:
            await outcome

    def fan_out(event: LineEvent) -> _LineHookOutcome:
        """Deliver one event to every hook, in registration order."""
        pending: list[cabc.Awaitable[None]] = []
        for hook in chain:
            outcome = hook(event)
            if outcome is not None:
                pending.append(outcome)
        if not pending:
            return None
        return await_all(pending)

    return fan_out
