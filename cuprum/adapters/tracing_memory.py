"""In-memory reference implementations of the tracing protocols.

``InMemorySpan`` and ``InMemoryTracer`` implement the ``Span`` and
``Tracer`` protocols from :mod:`cuprum.adapters.tracing_adapter` for
testing and examples. They store spans in memory for inspection and are
thread-safe, but are not intended for production telemetry.
"""

from __future__ import annotations

import dataclasses as dc
import threading
import typing as typ

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
class InMemoryTracer:
    """Reference in-memory tracer for testing and examples.

    This tracer stores spans in memory for inspection. It is thread-safe
    and suitable for unit testing but not for production use.

    Attributes
    ----------
    spans:
        List of all spans created by this tracer.

    """

    spans: list[InMemorySpan] = dc.field(default_factory=list)
    _lock: threading.Lock = dc.field(default_factory=threading.Lock, repr=False)

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

    def reset(self) -> None:
        """Clear all collected spans."""
        with self._lock:
            self.spans.clear()


__all__ = ["InMemorySpan", "InMemoryTracer"]
