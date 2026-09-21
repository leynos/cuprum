"""Shared scaffolding for the telemetry adapters.

This module owns the two pieces of structure the tracing, metrics, and
logging adapters previously duplicated:

- :func:`_event_common_fields`, the single source of truth for projecting an
  :class:`~cuprum.events.ExecEvent` into the common ``(key, value)`` pairs an
  adapter attaches to its backend records ("include the field only when it is
  not ``None``"). Adapters supply a key-naming function so backend-specific
  conventions (``cuprum.`` span attributes versus ``cuprum_`` log extras)
  stay local while the projection logic cannot drift.
- :class:`_LockedStore`, the lock-plus-guarded-``reset`` base shared by the
  in-memory reference collectors.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import threading
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent


_LOGGER = logging.getLogger("cuprum.adapters")


def _event_common_fields(
    event: ExecEvent,
    name: cabc.Callable[[str], str],
    *,
    argv: cabc.Callable[[tuple[str, ...]], object] = lambda value: value,
) -> cabc.Iterator[tuple[str, object]]:
    """Yield the canonical common projection of *event* as key/value pairs."""
    yield name("program"), str(event.program)
    yield name("argv"), argv(event.argv)
    if event.pid is not None:
        yield name("pid"), event.pid
    if event.cwd is not None:
        # A ``Path``, rendered rather than passed through.
        yield name("cwd"), str(event.cwd)
    if event.resource_usage_mode is not None:
        # A ``StrEnum``, rendered for the same reason: ``str`` yields the
        # member's value, and every transport this projection feeds — the log
        # extras, the span attributes, and the metric label — must carry the
        # plain string operators key on, not the member's ``repr``.
        yield name("resource_usage_mode"), str(event.resource_usage_mode)
    for field, value in _verbatim_fields(event):
        if value is not None:
            yield name(field), value


def _verbatim_fields(event: ExecEvent) -> tuple[tuple[str, object], ...]:
    """Return the optional fields the projection carries through unchanged."""
    # Each is omitted when ``None``, like every other optional field: the
    # caller applies the same check it applies to the rest of the projection.
    # The mode itself is not here at all: it is a ``StrEnum``, so
    # ``_event_common_fields`` renders it rather than passing it through.
    return (
        ("exit_code", event.exit_code),
        ("duration_s", event.duration_s),
        ("stage_index", event.stage_index),
        ("stage_count", event.stage_count),
        ("line", event.line),
        # The terminal resource measurements sit alongside the lifecycle
        # fields on purpose: a consumer reads the figures and the mode that
        # names their source from one record, so it can never mistake one for
        # the other's explanation.
        ("max_rss_bytes", event.max_rss_bytes),
        ("user_cpu_seconds", event.user_cpu_seconds),
        ("system_cpu_seconds", event.system_cpu_seconds),
    )


def _prefixed(prefix: str) -> cabc.Callable[[str], str]:
    """Return a key-naming function that prepends ``prefix`` to field names."""

    def build(field: str) -> str:
        """Prefix ``field`` with the adapter's key convention."""
        return f"{prefix}{field}"

    return build


def _project_tag(event: ExecEvent) -> str | None:
    """Return the event's ``project`` tag as a string, or ``None`` if unset."""
    project = event.tags.get("project")
    return None if project is None else str(project)


def _log_unhandled_phase(adapter: str, phase: str) -> None:
    """Log a phase that has no semantics for an adapter."""
    _LOGGER.debug(
        "Ignoring unhandled %s adapter phase: %s",
        adapter,
        phase,
    )


def _log_span_eviction(adapter: str, *, evicted: int, active: int) -> None:
    """Report spans dropped because the active-span registry was full.

    An evicted span is ended as failed while its execution may still be
    running, so the trace it would have carried is lost. That is a silent
    failure without a signal, hence ``WARNING`` rather than ``DEBUG``. Only
    counts are recorded: span attributes carry command payloads, and the
    execution tokens are unbounded in cardinality.

    Parameters
    ----------
    adapter:
        Adapter reporting the eviction, for the message and the log field.
    evicted:
        How many spans were detached and ended by this eviction.
    active:
        How many spans remain registered afterwards.
    """
    _LOGGER.warning(
        "span_registry_overflow adapter=%s evicted=%s active=%s",
        adapter,
        evicted,
        active,
        extra={
            "cuprum_adapter": adapter,
            "cuprum_spans_evicted": evicted,
            "cuprum_spans_active": active,
        },
    )


@dc.dataclass(slots=True)
class _LockedStore:
    """Base for thread-safe in-memory reference collectors.

    Owns the lock and the lock-guarded :meth:`reset` shared by the in-memory
    collectors. Subclasses implement :meth:`_clear` to empty their own
    storage; it runs while the lock is held.

    Thread safety
    -------------
    The store is protected by a single :class:`threading.Lock`. Mutators in
    subclasses must acquire ``self._lock`` for every read-modify-write of the
    shared storage, mirroring :meth:`reset`. The reference collectors are
    suitable for unit testing but not for production use.
    """

    _lock: threading.Lock = dc.field(
        default_factory=threading.Lock,
        repr=False,
        compare=False,
    )

    def reset(self) -> None:
        """Clear all collected state under the lock."""
        with self._lock:
            self._clear()

    def _clear(self) -> None:
        """Empty the subclass storage; invoked while the lock is held."""
        raise NotImplementedError


__all__ = [
    "_LockedStore",
    "_event_common_fields",
    "_log_span_eviction",
    "_log_unhandled_phase",
    "_prefixed",
    "_project_tag",
]
