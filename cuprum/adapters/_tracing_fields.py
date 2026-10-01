"""What exit and ancillary events add to a span, and their field sets.

Split from ``cuprum.adapters.tracing_adapter`` so that module stays about
*driving* a span — start, observe, and close — while field selections live
here.
Two distinct projections attach to one span, and they are easy to confuse:

- :data:`_SPAN_FIELDS`, recorded as span events on ancillary phases, and
- :func:`write_exit_attributes`, written as span attributes on ``exit``.

They differ in both what they carry and when it can be read. The resource
figures only exist once the child has been reaped, so they cannot be part of the
attributes built at ``start``; the exit projection is the only place they can
land, and the only place that can distinguish a platform which cannot measure
from one whose samples went missing.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    from cuprum.adapters.tracing_adapter import Span
    from cuprum.events import ExecEvent

# Ancillary span-event fields distinguish expiry modes and bounded drain outcomes.
_SPAN_FIELDS = (
    "line",
    "operation",
    "error_type",
    "note",
    "timeout_s",
    "timeout_mode",
    "eof_grace_s",
    "pending_readers",
)

# Terminal child-resource fields, written from the child ``exit`` event. The
# mode lets a backend tell an attributable measurement from the CPU-only
# fallback and from a platform that measures nothing; the figures accompany it
# only where a source produced them, so a ``None`` is skipped rather than
# written as null.
_EXIT_RESOURCE_FIELDS = (
    "max_rss_bytes",
    "user_cpu_seconds",
    "system_cpu_seconds",
    "resource_usage_mode",
)


def write_exit_attributes(span: Span, event: ExecEvent) -> None:
    """Write actual child-exit attributes while the span remains open.

    One rule for all of them: an optional field is written where a value exists
    and skipped where it does not, so an absent figure stays absent rather than
    being materialized as a null the backend must then special-case. That keeps
    ``exit_code`` and ``duration_s`` behaving exactly as the resource figures
    that joined them do.

    Parameters
    ----------
    span:
        The span for this execution.
    event:
        The ``exit`` event carrying the child attributes.
    """
    if event.exit_code is not None:
        span.set_attribute("cuprum.exit_code", event.exit_code)
    if event.duration_s is not None:
        span.set_attribute("cuprum.duration_s", event.duration_s)
    for field in _EXIT_RESOURCE_FIELDS:
        value = getattr(event, field)
        if value is not None:
            span.set_attribute(f"cuprum.{field}", value)


__all__ = [
    "_SPAN_FIELDS",
    "write_exit_attributes",
]
