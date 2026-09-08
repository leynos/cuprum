"""Aggregate completion events for pure-Python stream operations."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import enum
import typing as typ

if typ.TYPE_CHECKING:
    from cuprum.events import ExecId


class StreamOperation(enum.StrEnum):
    """Closed kinds of completed pure-Python stream operation."""

    DRAIN = "stream_drain"
    PIPELINE_TRANSFER = "pipeline_transfer"


class StreamOperationOutcome(enum.StrEnum):
    """Closed terminal outcomes for aggregate stream operations."""

    EOF = "eof"
    CANCELLED = "cancelled"
    FAILED = "failed"
    DOWNSTREAM_CLOSED = "downstream_closed"
    POST_CLOSE_DRAIN_TIMEOUT = "post_close_drain_timeout"


@dc.dataclass(frozen=True, slots=True)
class StreamOperationEvent:
    """Aggregate telemetry for one completed pure-Python stream operation.

    Attributes
    ----------
    operation:
        Closed operation kind, distinguishing a command-stream drain from a
        pipeline transfer.
    outcome:
        Closed completion outcome. ``downstream_closed`` and
        ``post_close_drain_timeout`` identify the bounded post-close path.
    bytes_consumed:
        Total bytes returned by the reader, including bytes discarded after a
        downstream writer closes.
    read_operations:
        Number of completed reader calls, including the read that returns EOF.
    duration_s:
        Monotonic elapsed seconds from operation start through completion.
    exec_id:
        Existing pipeline-stage correlation token when the pump context safely
        provides one. Direct command-stream drains carry ``None``.

    """

    operation: StreamOperation
    outcome: StreamOperationOutcome
    bytes_consumed: int
    read_operations: int
    duration_s: float
    exec_id: ExecId | None = None


type StreamOperationHook = cabc.Callable[[StreamOperationEvent], None]


__all__ = [
    "StreamOperation",
    "StreamOperationEvent",
    "StreamOperationHook",
    "StreamOperationOutcome",
]
