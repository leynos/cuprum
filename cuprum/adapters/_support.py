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
    for field, value in _rendered_enum_fields(event):
        if value is not None:
            yield name(field), value
    for field, value in _verbatim_fields(event):
        if value is not None:
            yield name(field), value


def _rendered_enum_fields(
    event: ExecEvent,
) -> tuple[tuple[str, object], ...]:
    """Return every ``StrEnum`` field of the common projection, rendered.

    Grouped rather than inlined because they share one reason to be here.
    ``str`` on these yields the member's value, and every transport this
    projection feeds — the log extras, the span attributes, and the metric
    label — must carry the plain string operators key on rather than the
    member's ``repr``. Their values are closed sets chosen by cuprum, never
    caller strings, which is what makes them safe to use as labels.

    Returns
    -------
    tuple[tuple[str, object], ...]
        Each field's name, paired with its rendered value or ``None`` when
        the event carries no such field. The caller applies the shared
        omit-when-``None`` policy.
    """
    # Each present only when the event records one. The resource mode names
    # where a figure was measured; the env mode names the policy, never the
    # environment, because a replacement run whose child failed to resolve a
    # bare program name is indistinguishable from an overlay one without it;
    # the error category separates two standard-stream boundaries that raise
    # the same exception class, so a consumer must read the value rather than
    # the ``repr`` — it is ``None`` on every phase but ``stdio_error``, and
    # the adapters that emit it keep their own narrower field set.
    return (
        (
            "resource_usage_mode",
            None
            if event.resource_usage_mode is None
            else str(event.resource_usage_mode),
        ),
        ("env_mode", None if event.env_mode is None else str(event.env_mode)),
        (
            "error_category",
            None if event.error_category is None else str(event.error_category),
        ),
        (
            "terminal_outcome",
            None if event.terminal_outcome is None else str(event.terminal_outcome),
        ),
    )


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
    "_log_unhandled_phase",
    "_prefixed",
    "_project_tag",
]
