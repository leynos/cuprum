"""In-memory reference implementations of the tracing protocols.

``InMemorySpan`` and ``InMemoryTracer`` implement the ``Span`` and
``Tracer`` protocols from :mod:`cuprum.adapters.tracing_adapter` for
testing and examples. They store spans in memory for inspection and are
thread-safe, but are not intended for production telemetry.

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
    """Reference in-memory span for testing and examples."""

    name: str
    attributes: dict[str, object] = dc.field(default_factory=dict)
    events: list[tuple[str, dict[str, object]]] = dc.field(default_factory=list)
    status_ok: bool | None = None
    ended: bool = False

    def set_attribute(self, key: str, value: object) -> None:
        """Set a span attribute."""
        self.attributes[key] = value

    def add_event(
        self,
        name: str,
        attributes: cabc.Mapping[str, object] | None = None,
    ) -> None:
        """Add an event to the span."""
        self.events.append((name, dict(attributes) if attributes else {}))

    def set_status(self, *, ok: bool) -> None:
        """Set the span status."""
        self.status_ok = ok

    def end(self) -> None:
        """End the span."""
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
        """Start a new in-memory span."""
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
