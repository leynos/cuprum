"""Structured lifecycle events for ``SafeCmd.lines()`` execution.

Line-stream lifecycle does not extend :class:`cuprum.events.ExecPhase`: that
is a closed public contract consumed exhaustively by existing adapters. This
module instead carries the bounded operational facts particular to the queue
and callback path behind ``SafeCmd.lines()``. ``exec_id`` correlates an event
with the existing execution lifecycle, while ``pid`` is available after spawn;
neither is suitable for a metric label.

Examples
--------
Use the lifecycle vocabulary to distinguish a normal completion from a
timeout or cancellation when handling a :class:`LineStreamEvent`::

    if event.phase is LineStreamPhase.COMPLETED:
        record_completion(event.exec_id)
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import enum
import typing as typ

if typ.TYPE_CHECKING:
    from cuprum.events import ExecId
    from cuprum.lines import LineStreamName


class LineStreamPhase(enum.StrEnum):
    """The bounded lifecycle vocabulary for :class:`LineStreamEvent`.

    Attributes
    ----------
    SPAWNED
        The subprocess and its consumers are ready.
    QUEUE_SATURATED
        A line sink is waiting for bounded queue capacity.
    SINK_FAILED
        A callback or queue sink raised while receiving a line.
    TIMEOUT
        The command deadline expired.
    CANCELLED
        Iteration or line delivery was cancelled.
    TEARDOWN_STARTED
        Shared child-process teardown began.
    TEARDOWN_COMPLETED
        Shared teardown and consumer drainage completed.
    COMPLETED
        The command result was assembled and is ready to publish.
    """

    SPAWNED = "spawned"
    QUEUE_SATURATED = "queue_saturated"
    SINK_FAILED = "sink_failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    TEARDOWN_STARTED = "teardown_started"
    TEARDOWN_COMPLETED = "teardown_completed"
    COMPLETED = "completed"


type LineStreamSink = typ.Literal["callback", "queue"]
"""Which line-delivery sink reported a failure."""


@dc.dataclass(frozen=True, slots=True)
class LineStreamEvent:
    """One line-stream lifecycle boundary for operational observers.

    ``queue_size`` and ``queue_capacity`` describe bounded backpressure only
    in structured event data. Metrics adapters use only the closed ``phase``,
    ``stream``, and ``sink`` vocabularies, never identifiers, payloads, or
    exception types.

    Attributes
    ----------
    phase
        One bounded lifecycle state from :class:`LineStreamPhase`.
    exec_id
        The execution identifier shared with the corresponding ``ExecEvent``
        lifecycle.
    pid
        The child process identifier once spawning has completed.
    stream
        The output stream related to a queue or sink event, when applicable.
    sink
        The bounded callback or queue destination related to a sink failure.
    queue_size
        The queue depth at a saturation boundary, recorded as event data only.
    queue_capacity
        The finite queue capacity at a saturation boundary.
    error_type
        The exception class name from a sink failure, never a metric label.
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
