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
import threading
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent


def _event_common_fields(
    event: ExecEvent,
    name: cabc.Callable[[str], str],
) -> cabc.Iterator[tuple[str, object]]:
    """Yield the canonical common projection of *event* as key/value pairs.

    ``program`` and ``argv`` are always yielded; ``pid``, ``cwd``,
    ``exit_code``, ``duration_s``, and ``line`` are yielded only when the
    event carries them (not ``None``). ``program`` and ``cwd`` are rendered
    as strings; ``argv`` is yielded as the original tuple, so adapters that
    need another container convert locally.

    Parameters
    ----------
    event:
        The execution event to project.
    name:
        Key-naming function mapping a canonical field name (for example,
        ``"pid"``) to the adapter's key (for example, ``"cuprum.pid"``).

    Example
    -------
    >>> dict(_event_common_fields(event, _prefixed("cuprum_")))  # doctest: +SKIP
    {'cuprum_program': 'echo', 'cuprum_argv': ('echo', 'hi'), 'cuprum_pid': 42}
    """
    yield name("program"), str(event.program)
    yield name("argv"), event.argv
    if event.pid is not None:
        yield name("pid"), event.pid
    if event.cwd is not None:
        yield name("cwd"), str(event.cwd)
    if event.exit_code is not None:
        yield name("exit_code"), event.exit_code
    if event.duration_s is not None:
        yield name("duration_s"), event.duration_s
    if event.line is not None:
        yield name("line"), event.line


def _prefixed(prefix: str) -> cabc.Callable[[str], str]:
    """Return a key-naming function that prepends ``prefix`` to field names."""

    def build(field: str) -> str:
        """Prefix ``field`` with the adapter's key convention."""
        return f"{prefix}{field}"

    return build


def _project_tag(event: ExecEvent) -> str | None:
    """Return the event's ``project`` tag as a string, or ``None`` if unset."""
    if "project" in event.tags:
        return str(event.tags["project"])
    return None


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

    _lock: threading.Lock = dc.field(default_factory=threading.Lock, repr=False)

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
    "_prefixed",
    "_project_tag",
]
