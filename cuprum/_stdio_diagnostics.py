"""Bounded operational diagnostics for the standard-stream failure boundaries.

Every place cuprum fails while setting up or driving a child's standard streams
reports through :func:`_emit_stdio_error`: the caller's producer, an invalid
chunk, the encoder, the child's own pipe, opening a cuprum-owned target file,
and flushing a caller's borrowed file object. They share one emitter because
they share one payload policy, and expressing it once is what makes a boundary
added later inherit it rather than re-derive it.

The policy has three parts.

*The boundary is named, not inferred.* The record carries a bounded
:class:`~cuprum.stdio_events.StdioFailureCategory` and a stable operation. Both
are passed rather than derived from the exception, because the exception cannot
distinguish these boundaries: advancing a producer and writing to the child
both raise ``OSError`` in the pipe family, an invalid chunk and a mistyped
encoder both raise ``TypeError``, and a producer's own ``BrokenPipeError`` is
indistinguishable at the type level from the child closing its end.

*Nothing else travels with it.* No exception message, traceback, path, payload,
argv, or caller tag. All of those are caller data, and this is a signal about
the failure rather than the channel that carries it — the caller still receives
the full exception, message and chained cause intact. The pre-spawn boundaries
are the sharp case: :func:`~cuprum._stdio_plan._open_owned_path` deliberately
puts the failing path in the ``OSError`` it raises, and that message must reach
the caller while staying out of the diagnostic.

*``pid`` is carried only once a child exists.* Opening an owned target file and
flushing a borrowed one both run before the fork, so they pass ``None``. The
parameter is required rather than defaulted, which makes every call site state
which side of the spawn it is on instead of inventing a value.

Both channels are best-effort. These run where a failure, or a cancellation, is
already in flight, and a logging handler, an observe hook, or a metric
collector must never become the outcome the caller sees.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import logging
import typing as typ

from cuprum._pipeline_types import _EventDetails
from cuprum._timeout_reporting import _safe_emit

if typ.TYPE_CHECKING:
    from cuprum._pipeline_types import _StageObservation
    from cuprum.stdio_events import StdioFailureCategory

# ``cuprum.stdio`` rather than a module-path name, mirroring ``cuprum.stdin``
# and ``cuprum.timeout``: these are stable logger names an observability
# integration keys on, and one derived from this module's path would move with
# the code that emits through it.
_LOGGER = logging.getLogger("cuprum.stdio")


@dc.dataclass(frozen=True, slots=True)
class _StdioFailure:
    """The bounded payload every stdio diagnostic carries.

    Grouping these four fields is what makes the payload policy a property of
    one type rather than a convention restated at each call site: the log
    record and the observe event are two projections of this object, so a
    field added here reaches both channels or neither. It also keeps the
    emitter itself down to the two things it actually needs — where to emit,
    and what to say.

    Parameters
    ----------
    category : StdioFailureCategory
        Which boundary failed. See the module docstring for why it is passed
        rather than inferred.
    operation : str
        The stable operation name for this boundary, such as ``"write"`` or
        ``"open"``. It is a fixed identifier chosen at the call site, never
        caller prose.
    error_type : str
        The failing exception's class name, for consumers that already filter
        on the existing ``cuprum_error_type`` field.
    pid : int | None
        The child's process identifier, or ``None`` for a boundary that fails
        before the fork.
    """

    category: StdioFailureCategory
    operation: str
    error_type: str
    pid: int | None


def _emit_stdio_error(
    observation: _StageObservation,
    failure: _StdioFailure,
) -> None:
    """Emit a best-effort ``stdio_error`` diagnostic for a failing boundary.

    The log record and the observe event carry the same bounded payload, so a
    consumer reading either sees the same boundary name. Each is guarded
    separately: both run where another failure is already propagating, and
    neither a logging handler nor an observe hook may become the outcome the
    caller sees.

    Parameters
    ----------
    observation : _StageObservation
        The stage to emit the observe event on.
    failure : _StdioFailure
        The bounded boundary, operation, error class, and process identifier
        this diagnostic reports.
    """
    logging_extra = {
        "cuprum_pid": failure.pid,
        "cuprum_operation": failure.operation,
        "cuprum_error_type": failure.error_type,
        "cuprum_error_category": str(failure.category),
    }
    with contextlib.suppress(Exception, asyncio.CancelledError):
        _LOGGER.error(
            "stdio_%s_failed category=%s pid=%s error=%s",
            failure.operation,
            failure.category,
            failure.pid,
            failure.error_type,
            extra=logging_extra,
        )
    _safe_emit(
        observation,
        "stdio_error",
        _EventDetails(
            pid=failure.pid,
            operation=failure.operation,
            error_type=failure.error_type,
            error_category=failure.category,
        ),
    )


__all__ = ["_StdioFailure", "_emit_stdio_error"]
