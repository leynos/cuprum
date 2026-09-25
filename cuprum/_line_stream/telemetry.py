"""Correlated lifecycle telemetry for one ``SafeCmd.lines()`` run.

Part of the ``cuprum._line_stream`` package. The telemetry types carry no
coupling to the run's spawn or teardown mechanics; they only translate
lifecycle boundaries into bounded
:class:`~cuprum.line_stream_events.LineStreamEvent` records.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum.line_stream_events import LineStreamEvent, LineStreamPhase
from cuprum.line_stream_observation import _emit_line_stream_event

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._line_stream.line_queue import _LineQueueItem
    from cuprum.events import ExecId
    from cuprum.line_stream_events import LineStreamSink
    from cuprum.lines import LineEvent, LineStreamName

__all__ = ["_LineStreamEventDetails", "_LineStreamTelemetry"]


@dc.dataclass(frozen=True, slots=True)
class _LineStreamEventDetails:
    """Optional bounded fields attached to one line-stream lifecycle event."""

    stream: LineStreamName | None = None
    sink: LineStreamSink | None = None
    error: BaseException | None = None
    queue_size: int | None = None


@dc.dataclass(slots=True)
class _LineStreamTelemetry:
    """Emit correlated, bounded lifecycle details for one line-stream run."""

    exec_id: ExecId
    queue_capacity: int
    pid: int | None = None
    is_queue_saturated: bool = False

    def emit(
        self,
        phase: LineStreamPhase,
        details: _LineStreamEventDetails | None = None,
    ) -> None:
        """Publish one lifecycle boundary without carrying decoded text."""
        event_details = details or _LineStreamEventDetails()
        _emit_line_stream_event(
            LineStreamEvent(
                phase=phase,
                exec_id=self.exec_id,
                pid=self.pid,
                stream=event_details.stream,
                sink=event_details.sink,
                queue_size=event_details.queue_size,
                queue_capacity=(
                    self.queue_capacity
                    if event_details.queue_size is not None
                    else None
                ),
                error_type=(
                    type(event_details.error).__name__
                    if event_details.error is not None
                    else None
                ),
            )
        )

    def report_queue_saturation(
        self,
        queue: asyncio.Queue[_LineQueueItem],
        event: LineEvent,
    ) -> None:
        """Report a transition into bounded queue backpressure."""
        if queue.full() and not self.is_queue_saturated:
            self.is_queue_saturated = True
            self.emit(
                LineStreamPhase.QUEUE_SATURATED,
                _LineStreamEventDetails(
                    stream=event.stream,
                    queue_size=queue.qsize(),
                ),
            )
