"""Backend-agnostic tracing protocols.

Split from ``cuprum.adapters.tracing_adapter`` so that module stays about the
hook itself — span lifecycle, correlation, and the bounded registry of open
spans — while the structural contract a backend must satisfy lives here.

``cuprum.adapters.tracing_adapter`` re-exports both names, so the documented
``from cuprum.adapters.tracing_adapter import Tracer, Span`` import still
works.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class Span(typ.Protocol):
    """Protocol for a tracing span.

    Spans represent a unit of work (in this case, command execution) and
    can be enriched with attributes and events.
    """

    def set_attribute(self, key: str, value: object) -> None:
        """Set a span attribute.

        Parameters
        ----------
        key:
            Attribute name (e.g., ``cuprum.program``).
        value:
            Attribute value (string, int, float, bool, or list thereof).

        """
        # PEP 544 protocol stub.
        ...  # pylint: disable=unnecessary-ellipsis

    def add_event(
        self,
        name: str,
        attributes: cabc.Mapping[str, object] | None = None,
    ) -> None:
        """Add an event to the span.

        Parameters
        ----------
        name:
            Event name (e.g., ``cuprum.stdout``).
        attributes:
            Optional attributes for the event.

        """
        # PEP 544 protocol stub.
        ...  # pylint: disable=unnecessary-ellipsis

    def set_status(self, *, ok: bool) -> None:
        """Set the span status.

        Parameters
        ----------
        ok:
            True if the operation succeeded, False otherwise.

        """
        # PEP 544 protocol stub.
        ...  # pylint: disable=unnecessary-ellipsis

    def end(self) -> None:
        """End the span, recording its duration."""
        # PEP 544 protocol stub.
        ...  # pylint: disable=unnecessary-ellipsis


class Tracer(typ.Protocol):
    """Protocol for a tracing backend.

    Implementations must be thread-safe; hooks may be invoked from multiple
    threads or async tasks concurrently.
    """

    def start_span(
        self,
        name: str,
        attributes: cabc.Mapping[str, object] | None = None,
    ) -> Span:
        """Start a new span.

        Parameters
        ----------
        name:
            Span name (e.g., ``cuprum.exec echo``).
        attributes:
            Initial span attributes.

        Returns
        -------
        Span
            A span that must be ended by calling :meth:`Span.end`.

        """
        # PEP 544 protocol stub.
        ...  # pylint: disable=unnecessary-ellipsis


__all__ = ["Span", "Tracer"]
