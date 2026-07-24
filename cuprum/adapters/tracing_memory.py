"""In-memory reference implementations of the tracing protocols.

``InMemorySpan`` and ``InMemoryTracer`` implement the ``Span`` and
``Tracer`` protocols from :mod:`cuprum.adapters.tracing_adapter` for
testing and examples. They store spans in memory for inspection and are
not intended for production telemetry. ``InMemoryTracer`` protects its
span store with a lock (see below); the ``InMemorySpan`` objects it
returns are plain mutable test doubles that provide no synchronization of
their own.

``InMemoryTracer`` follows the shared
:class:`~cuprum.adapters._support._LockedStore` contract: every mutator
holds the lock, and ``reset()`` clears the store under it.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum.adapters._support import _LockedStore

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@dc.dataclass(slots=True)
class InMemorySpan:
    """Reference in-memory span for testing and examples.

    A mutable test double implementing the ``Span`` protocol from
    :mod:`cuprum.adapters.tracing_adapter`. It records everything set on it so
    that tests can inspect the result. It performs no synchronization of its
    own; a span is expected to be mutated by a single thread at a time.

    Attributes
    ----------
    name : str
        Name of the span, as supplied to
        :meth:`InMemoryTracer.start_span`.
    attributes : dict[str, object]
        Span attributes keyed by name. Seeded from the attributes passed at
        creation and extended in place by :meth:`set_attribute`.
    events : list[tuple[str, dict[str, object]]]
        Recorded events in insertion order, each a ``(name, attributes)``
        pair appended by :meth:`add_event`.
    status_ok : bool | None
        Completion status: ``None`` until :meth:`set_status` is called, then
        ``True`` for success or ``False`` for failure.
    ended : bool
        Whether :meth:`end` has been called; ``False`` until then.
    """

    name: str
    attributes: dict[str, object] = dc.field(default_factory=dict)
    events: list[tuple[str, dict[str, object]]] = dc.field(default_factory=list)
    status_ok: bool | None = None
    ended: bool = False

    def set_attribute(self, key: str, value: object) -> None:
        """Record a single span attribute, overwriting any existing value.

        Parameters
        ----------
        key : str
            Attribute name (for example, ``"cuprum.program"``).
        value : object
            Attribute value, stored verbatim.

        Returns
        -------
        None
            The span's :attr:`attributes` mapping is mutated in place.
        """
        self.attributes[key] = value

    def add_event(
        self,
        name: str,
        attributes: cabc.Mapping[str, object] | None = None,
    ) -> None:
        """Append a named event to the span.

        Parameters
        ----------
        name : str
            Event name (for example, ``"cuprum.stdout"``).
        attributes : collections.abc.Mapping[str, object] | None, optional
            Optional event attributes. When provided, a shallow copy is
            stored so later caller mutations do not affect the recorded
            event; when ``None`` (the default), an empty attribute mapping is
            recorded.

        Returns
        -------
        None
            The new event is appended to :attr:`events` in place.
        """
        self.events.append((name, dict(attributes) if attributes else {}))

    def set_status(self, *, ok: bool) -> None:
        """Set the span's completion status.

        Parameters
        ----------
        ok : bool
            Keyword-only. ``True`` marks the span as successful and ``False``
            marks it as failed; the value is stored on :attr:`status_ok`.

        Returns
        -------
        None
            :attr:`status_ok` is updated in place.
        """
        self.status_ok = ok

    def end(self) -> None:
        """Mark the span as ended.

        Returns
        -------
        None
            Sets :attr:`ended` to ``True``. No further recording is expected
            after a span has ended, though the double does not enforce this.
        """
        self.ended = True


@dc.dataclass(slots=True)
class InMemoryTracer(_LockedStore):
    """Reference in-memory tracer for testing and examples.

    Storage and locking follow the shared
    :class:`~cuprum.adapters._support._LockedStore` contract: every mutator
    holds the lock, and ``reset()`` clears the store under it.

    Attributes
    ----------
    spans:
        List of all spans created by this tracer.
    """

    spans: list[InMemorySpan] = dc.field(default_factory=list)

    def start_span(
        self,
        name: str,
        attributes: cabc.Mapping[str, object] | None = None,
    ) -> InMemorySpan:
        """Create, record, and return a new in-memory span.

        The span is appended to :attr:`spans` under the store lock, so
        concurrent ``start_span`` calls record their spans safely.

        Parameters
        ----------
        name : str
            Name for the new span.
        attributes : collections.abc.Mapping[str, object] | None, optional
            Optional initial span attributes. When provided, a shallow copy
            seeds the span's :attr:`~InMemorySpan.attributes`; when ``None``
            (the default), the span starts with no attributes.

        Returns
        -------
        InMemorySpan
            The newly created span, already appended to :attr:`spans`. The
            caller is responsible for ending it via :meth:`InMemorySpan.end`.
        """
        span = InMemorySpan(
            name=name,
            attributes=dict(attributes) if attributes else {},
        )
        with self._lock:
            self.spans.append(span)
        return span

    @typ.override
    def _clear(self) -> None:
        """Clear all collected spans; called under the store lock."""
        self.spans.clear()


__all__ = ["InMemorySpan", "InMemoryTracer"]
