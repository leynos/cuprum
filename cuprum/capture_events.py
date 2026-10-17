"""Structured events for per-stream capture policy outcomes.

Bounding a capture discards bytes, and spilling one moves the stream to a file
the parent must later remove. Both are observability facts about a run that
otherwise completes — the child is reaped, the caller gets a result — but
neither is a command lifecycle event, and :data:`~cuprum.events.ExecPhase` is a
closed set that registered consumers match exhaustively. Adding a phase would
raise inside consumers that were correct when they were written. A dedicated
event type on its own hook registry (:mod:`cuprum.capture_observation`) means a
consumer opts in by registering, and a consumer that does not is untouched.

The event carries only bounded values: which of the two streams it describes, a
closed-set category, byte counts, and an error *class name*. The subprocess
payload, a spill file's path, and any exception text never reach it — the same
rule :mod:`cuprum.echo_events` applies to echo sinks, and for the same reason:
a registered hook may forward these to a metrics backend whose labels are
low-cardinality by contract.
"""

from __future__ import annotations

import dataclasses as dc
import enum
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.capture_policy import CaptureStreamName


class CaptureEventCategory(enum.StrEnum):
    """Why a capture event occurred, as a closed set of categories.

    Examples
    --------
    The member value is the string operators see::

        assert CaptureEventCategory.TRUNCATED == "truncated"
        assert CaptureEventCategory.SPILLED_TO_FILE == "spilled_to_file"
    """

    TRUNCATED = "truncated"
    SPILLED_TO_FILE = "spilled_to_file"
    STORAGE_WRITE_FAILED = "storage_write_failed"
    STORAGE_FLUSH_FAILED = "storage_flush_failed"


@dc.dataclass(frozen=True, slots=True)
class CaptureEvent:
    """A per-stream capture outcome reported to registered capture hooks.

    One event describes one stream at one moment: a bounded capture that
    discarded bytes reports :attr:`dropped_bytes` once, at finalization, and a
    file-backed capture reports its spill once, at the transition. A failure
    to write or flush the spill file is reported when it happens, with the
    error's class in :attr:`error_type`; the run then completes normally, so
    the event is the only record of it beyond
    :attr:`~cuprum.capture_policy.CapturedStream.storage_failed`.

    Attributes
    ----------
    stream:
        Which output stream the capture outcome belonged to.
    category:
        The closed-set category naming what happened.
    total_bytes:
        Bytes the child wrote to this stream, when the event knows it.
    dropped_bytes:
        Bytes the policy discarded, for
        :attr:`CaptureEventCategory.TRUNCATED`.
    spilled_bytes:
        Bytes moved to the file, for
        :attr:`CaptureEventCategory.SPILLED_TO_FILE` and the two storage
        failures.
    error_type:
        The *name* of the exception class behind a storage failure, never its
        message and never the exception itself.

    Examples
    --------
    A bounded stdout capture that dropped sixteen bytes::

        event = CaptureEvent(
            stream="stdout",
            category=CaptureEventCategory.TRUNCATED,
            total_bytes=32,
            dropped_bytes=16,
        )
        assert event.dropped_bytes == 16
    """

    stream: CaptureStreamName
    category: CaptureEventCategory
    total_bytes: int | None = None
    dropped_bytes: int | None = None
    spilled_bytes: int | None = None
    error_type: str | None = None


type CaptureHook = cabc.Callable[[CaptureEvent], None]
"""A synchronous consumer of :class:`CaptureEvent` values.

Capture hooks are synchronous by contract, as echo hooks are: the emission
sites sit inside the drain's per-chunk and finalization paths, and there is no
point at which awaiting a hook could be ordered against the chunk being
captured.
"""


__all__ = [
    "CaptureEvent",
    "CaptureEventCategory",
    "CaptureHook",
]
