"""The bounded queue and run record behind one ``SafeCmd.lines()`` iteration.

Split out of ``cuprum._line_stream`` to keep that module within the
repository's line-count limit. This module owns the queue item type, its
capacity, the spawned-run record every coordinator step reads and updates,
and the sink/hook wrappers that feed the queue from the stream consumers.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum._line_stream_telemetry import _LineStreamEventDetails
from cuprum.line_stream_events import LineStreamPhase

if typ.TYPE_CHECKING:
    from cuprum._line_stream_telemetry import _LineStreamTelemetry
    from cuprum._subprocess_wait import _RunTaskOwnership
    from cuprum.line_stream_events import LineStreamSink
    from cuprum.lines import LineEvent, _LineHookFn
    from cuprum.sh import CommandResult

__all__ = [
    "_LINE_QUEUE_CAPACITY",
    "_LineQueueItem",
    "_LineStreamRun",
    "_line_event_queue",
    "_observed_line_hook",
    "_queue_line_sink",
]

# The queue item is a line event while the run streams, and the run's
# ``CommandResult`` exactly once, when the coordinator finishes; the result
# doubles as the sentinel that ends iteration.
type _LineQueueItem = LineEvent | CommandResult

# Finite on purpose. The sink awaits ``queue.put``, so a caller iterating
# slowly stops the consumers reading the pipe instead of letting a chatty child
# grow the queue without bound.
_LINE_QUEUE_CAPACITY = 256


@dc.dataclass(frozen=True, slots=True)
class _LineStreamRun:
    """The spawned pieces one ``lines()`` iteration coordinates.

    Attributes
    ----------
    process:
        The subprocess whose stdout and stderr feed the queue.
    tasks:
        Ownership of the stdin writer and the stream consumers, so the shared
        reconciliation cancels and drains each exactly once.
    queue:
        The queue every ``LineEvent`` is posted to; the sentinel ends
        iteration.
    started_at:
        Monotonic reference the per-line ``at`` stamps are measured from.

    """

    process: asyncio.subprocess.Process
    tasks: _RunTaskOwnership
    queue: asyncio.Queue[_LineQueueItem]
    started_at: float
    telemetry: _LineStreamTelemetry


def _line_event_queue() -> asyncio.Queue[_LineQueueItem]:
    """Return the finite queue one ``lines()`` iteration consumes."""
    return asyncio.Queue(maxsize=_LINE_QUEUE_CAPACITY)


def _queue_line_sink(
    queue: asyncio.Queue[_LineQueueItem],
    telemetry: _LineStreamTelemetry | None = None,
) -> _LineHookFn:
    """Return an asynchronous hook that posts each ``LineEvent`` to the queue.

    Asynchronous rather than a synchronous ``put_nowait`` because the queue is
    finite: a full queue parks the stream consumer until the iterator drains a
    slot, so ``lines()`` applies backpressure to the child instead of dropping
    events or retaining them without limit. A synchronous sink could only
    raise ``asyncio.QueueFull`` out of the drain loop.

    Returns
    -------
    _LineHookFn
        The hook that posts one event to *queue*, awaiting a free slot when the
        queue is full.
    """

    async def enqueue(event: LineEvent) -> None:
        """Post one stamped line to the consumer queue."""
        if telemetry is not None:
            telemetry.report_queue_saturation(queue, event)
        await queue.put(event)
        if telemetry is not None:
            telemetry.is_queue_saturated = queue.full()

    return enqueue


def _observed_line_hook(
    hook: _LineHookFn,
    sink: LineStreamSink,
    telemetry: _LineStreamTelemetry,
) -> _LineHookFn:
    """Wrap one delivery hook so its failure is correlated before it escapes."""

    async def deliver(event: LineEvent) -> None:
        """Deliver one event while preserving the hook's failure semantics."""
        try:
            outcome = hook(event)
            if outcome is not None:
                await outcome
        except asyncio.CancelledError:
            telemetry.emit(
                LineStreamPhase.CANCELLED,
                _LineStreamEventDetails(stream=event.stream, sink=sink),
            )
            raise
        except BaseException as error:
            telemetry.emit(
                LineStreamPhase.SINK_FAILED,
                _LineStreamEventDetails(
                    stream=event.stream,
                    sink=sink,
                    error=error,
                ),
            )
            raise

    return deliver
