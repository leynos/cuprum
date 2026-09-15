"""Bounded metrics projection for line-stream lifecycle events."""

from __future__ import annotations

import typing as typ

from cuprum.line_stream_events import (
    LineStreamEvent,
    LineStreamHook,
    LineStreamPhase,
    LineStreamSink,
)

if typ.TYPE_CHECKING:
    from cuprum.adapters.metrics_adapter import MetricsCollector

LINE_STREAM_EVENTS_TOTAL = "cuprum_line_stream_events_total"
"""Counter of ``SafeCmd.lines()`` lifecycle events by closed categories."""

_UNKNOWN = "unknown"
_NONE = "none"
_PHASES = frozenset(typ.get_args(LineStreamPhase))
_STREAMS = frozenset(("stdout", "stderr"))
_SINKS = frozenset(typ.get_args(LineStreamSink))


def _label(value: object | None, allowed: frozenset[str]) -> str:
    """Return a fixed metric label without trusting public event annotations."""
    if value is None:
        return _NONE
    return str(value) if value in allowed else _UNKNOWN


class LineStreamMetricsHook:
    """Count line-stream lifecycle events with bounded categorical labels."""

    def __init__(self, collector: MetricsCollector) -> None:
        """Store the thread-safe metrics collector."""
        self._collector = collector

    def __call__(self, event: LineStreamEvent) -> None:
        """Project one event without using unbounded correlation or error fields."""
        self._collector.inc_counter(
            LINE_STREAM_EVENTS_TOTAL,
            1.0,
            {
                "phase": _label(event.phase, _PHASES),
                "stream": _label(event.stream, _STREAMS),
                "sink": _label(event.sink, _SINKS),
            },
        )


def line_stream_metrics_hook(collector: MetricsCollector) -> LineStreamHook:
    """Return a lifecycle hook that projects events into ``collector``."""
    return LineStreamMetricsHook(collector)


__all__ = [
    "LINE_STREAM_EVENTS_TOTAL",
    "LineStreamMetricsHook",
    "line_stream_metrics_hook",
]
