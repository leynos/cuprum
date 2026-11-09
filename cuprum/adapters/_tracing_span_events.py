"""Shared span-event projection for the tracing adapter's correlated hooks.

Every correlated hook needs the same preamble: find the execution's open span
by its correlation token under the hook's lifecycle lock, bail out when the
execution has no open span, then append the event under that span's own lock
without touching a span that has already closed. Keeping that sequence in one
place means the lifetime rule — never add an event to a closed span — cannot
drift between the hooks that rely on it.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.adapters.tracing_adapter import TracingHook
    from cuprum.events import ExecId


@dc.dataclass(frozen=True, slots=True)
class _SpanEvent:
    """One span event, and whether it is bounded to one per execution."""

    name: str
    attributes: cabc.Mapping[str, object]
    once: bool = False


def record_span_event(
    hook: TracingHook,
    exec_id: ExecId,
    name: str,
    attributes: cabc.Mapping[str, object],
) -> None:
    """Add ``name`` with ``attributes`` to the open span for ``exec_id``.

    Parameters
    ----------
    hook:
        The tracing hook that owns the open-span registry.
    exec_id:
        Correlation token identifying the execution the event belongs to.
    name:
        Span event name, a fixed ``cuprum.*`` value.
    attributes:
        Bounded attributes describing the event.

    Notes
    -----
    The registry lock is held only long enough to read the entry. A production
    span may block on I/O in ``add_event``, so holding the lifecycle lock
    across the append would serialize every other execution's handler.
    """
    _append_span_event(hook, exec_id, _SpanEvent(name, attributes))


def record_span_event_once(
    hook: TracingHook,
    exec_id: ExecId,
    name: str,
    attributes: cabc.Mapping[str, object],
) -> None:
    """Add ``name`` to the open span for ``exec_id`` at most once.

    Use this for a decision taken on the per-line path, where every repetition
    would otherwise grow the span with the line count. The counters that need
    every occurrence live in the metrics projection instead. The lifetime and
    locking rules are those of :func:`record_span_event`.
    """
    _append_span_event(hook, exec_id, _SpanEvent(name, attributes, once=True))


def _append_span_event(hook: TracingHook, exec_id: ExecId, event: _SpanEvent) -> None:
    """Append ``event`` to the execution's open span, if it has one."""
    with hook._lock:
        active = hook._active_spans.get(exec_id)
    if active is None:
        return
    with active.lock:
        if active.is_closed:
            return
        if event.once and event.name in active.once_events:
            return
        active.span.add_event(event.name, event.attributes)
        if event.once:
            active.once_events.add(event.name)
