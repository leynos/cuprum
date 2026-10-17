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
    yield from _optional_common_fields(event, name)


def _optional_common_fields(
    event: ExecEvent,
    name: cabc.Callable[[str], str],
) -> cabc.Iterator[tuple[str, object]]:
    """Yield *event*'s optional common fields, omitting the absent ones.

    The omit-when-``None`` policy is applied once, here, to every optional
    field; the fields differ only in how each value is transported, which is
    what the two groups below separate. Concatenating them preserves the
    projection's field order: the process identity, then the rendered
    ``StrEnum`` modes, then the verbatim measurements.

    Yields
    ------
    tuple[str, str]
        Each present field's backend-specific name, paired with its value.
        Absent fields yield nothing at all rather than a ``None``.
    """
    optional = (
        ("pid", event.pid),
        # A ``Path``, rendered rather than passed through.
        ("cwd", None if event.cwd is None else str(event.cwd)),
        # Terminal figures carry an outcome rather than a mode, so they sit
        # outside the enum group despite being rendered on the same rule.
        ("terminal_outcome", _rendered_value(event.terminal_outcome)),
        # Both groups below return bare names, which this loop names once, so
        # each field's key is built in exactly one place.
        *_rendered_enum_fields(event),
        *_verbatim_fields(event),
    )
    for field, value in optional:
        if value is not None:
            yield name(field), value


def _rendered_enum_fields(event: ExecEvent) -> tuple[tuple[str, object], ...]:
    """Return each enum-valued field of the projection, rendered or ``None``.

    Every one is a ``StrEnum``, so ``str`` yields the member's value: each
    transport this projection feeds — the log extras, the span attributes, and
    the metric label — must carry the plain string operators key on, not the
    member's ``repr``.

    The three modes describe the run rather than one measurement, so each is
    carried on every phase. ``error_category`` is the exception and is listed
    last for that reason: it describes a single boundary failure, so it is set
    on ``stdio_error`` alone — the adapters that emit it keep their own
    narrower field set beside this projection.

    Returned rather than yielded so the caller's single omit-when-``None``
    loop stays the one place that policy lives; names are returned bare so the
    caller is likewise the only place a key is built.

    Returns
    -------
    tuple[tuple[str, object], ...]
        Each field's bare name paired with its rendered value, or with
        ``None`` when the event records no such field.
    """
    return (
        # How the terminal figures were obtained, when they were.
        _rendered("resource_usage_mode", event.resource_usage_mode),
        # The environment policy, which a replacement boundary makes
        # load-bearing: without it a child that failed to resolve a bare
        # program name is indistinguishable from an ordinary overlay run.
        _rendered("env_mode", event.env_mode),
        # The mode the run reports its captures in, which is what keeps a
        # byte-exact run distinguishable from a text one in every counter and
        # span attribute.
        _rendered("capture_mode", event.capture_mode),
        # The field that separates two standard-stream boundaries raising the
        # same exception class, so a consumer must read the value rather than
        # the member's ``repr``.
        _rendered("error_category", event.error_category),
    )


def _rendered_value(value: object) -> object:
    """Render *value*, or return ``None`` when the event records none."""
    return None if value is None else str(value)


def _rendered(field: str, value: object) -> tuple[str, object]:
    """Pair *field* with *value* rendered, or with ``None`` when unset."""
    return field, _rendered_value(value)


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
        # The executed executable, omitted when unbound. It is the one field
        # here that names what actually ran rather than what was measured, so
        # it travels with ``program`` in spirit — but it belongs in the
        # verbatim group because it is a plain optional string, not a value
        # needing rendering.
        ("resolved_path", event.resolved_path),
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
