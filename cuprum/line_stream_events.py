"""Structured lifecycle events for ``SafeCmd.lines()`` execution.

Line-stream lifecycle does not extend :class:`cuprum.events.ExecPhase`: that
is a closed public contract consumed exhaustively by existing adapters. This
module instead carries the bounded operational facts particular to the queue
and callback path behind ``SafeCmd.lines()``. ``exec_id`` correlates an event
with the existing execution lifecycle, while ``pid`` is available after spawn;
neither is suitable for a metric label.
"""

import collections.abc as cabc
import dataclasses as dc
import typing as typ

from cuprum.events import ExecId
from cuprum.lines import LineStreamName

type LineStreamPhase = typ.Literal[
    "spawned",
    "queue_saturated",
    "sink_failed",
    "timeout",
    "cancelled",
    "teardown_started",
    "teardown_completed",
    "completed",
]
"""The bounded lifecycle vocabulary for :class:`LineStreamEvent`."""

type LineStreamSink = typ.Literal["callback", "queue"]
"""Which line-delivery sink reported a failure."""


@dc.dataclass(frozen=True, slots=True)
class LineStreamEvent:
    """One line-stream lifecycle boundary for operational observers.

    ``queue_size`` and ``queue_capacity`` describe bounded backpressure only
    in structured event data. Metrics adapters use only the closed ``phase``,
    ``stream``, and ``sink`` vocabularies, never identifiers, payloads, or
    exception types.
    """

    phase: LineStreamPhase
    exec_id: ExecId
    pid: int | None
    stream: LineStreamName | None = None
    sink: LineStreamSink | None = None
    queue_size: int | None = None
    queue_capacity: int | None = None
    error_type: str | None = None


type LineStreamHook = cabc.Callable[[LineStreamEvent], None]
"""A synchronous observer of :class:`LineStreamEvent` values."""


__all__ = [
    "LineStreamEvent",
    "LineStreamHook",
    "LineStreamPhase",
    "LineStreamSink",
]
