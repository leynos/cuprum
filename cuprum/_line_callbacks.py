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
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum.lines import LineEvent, LineHook, LineStreamName, perf_counter

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import _StageObservation


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
    on_line: LineHook | None
    started_at: float


def _compose_line_callbacks(
    observation: _StageObservation,
    context: _LineEmissionContext,
) -> cabc.Callable[[str], None] | None:
    """Compose the observe-hook emission and the user ``on_line`` per line.

    Returns
    -------
    collections.abc.Callable[[str], None] | None
        A callback invoked once per decoded line, or ``None`` when neither the
        observe hooks nor a user callback needs the stream, preserving the
        zero-cost no-line-observer path.
    """
    has_observe_hooks = bool(observation.hooks.observe_hooks)
    if not has_observe_hooks and context.on_line is None:
        return None

    def emit_line(line: str) -> None:
        """Fan one decoded line out to the observe hooks and the user callback."""
        if has_observe_hooks:
            observation.emit(
                context.stream,
                _EventDetailsShim(pid=context.pid, line=line).details,
            )
        if context.on_line is not None:
            context.on_line(
                _stamp_line(
                    line,
                    stream=context.stream,
                    started_at=context.started_at,
                ),
            )

    return emit_line


class _EventDetailsShim:
    """Build the event details without importing the pipeline type eagerly."""

    __slots__ = ("details",)

    def __init__(self, *, pid: int | None, line: str) -> None:
        """Construct the ``_EventDetails`` payload for one line event."""
        from cuprum._pipeline_types import _EventDetails

        self.details = _EventDetails(pid=pid, line=line)
