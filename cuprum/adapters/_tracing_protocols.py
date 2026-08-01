"""Protocol definitions for the tracing adapter.

These protocols keep :mod:`cuprum.adapters.tracing_adapter` decoupled from any
specific tracing library. Projects can implement :class:`Span` and
:class:`Tracer` with their preferred backend (OpenTelemetry, Jaeger, Zipkin,
etc.).
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
        # PEP 544 protocol stub; the docstring is the body.

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
        # PEP 544 protocol stub; the docstring is the body.

    def set_status(self, *, ok: bool) -> None:
        """Set the span status.

        Parameters
        ----------
        ok:
            True if the operation succeeded, False otherwise.

        """
        # PEP 544 protocol stub; the docstring is the body.

    def end(self) -> None:
        """End the span, recording its duration."""
        # PEP 544 protocol stub; the docstring is the body.


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
        # PEP 544 protocol stub; the docstring is the body.
