"""The backend contract the tracing adapter is written against.

``Span`` and ``Tracer`` are the whole of what
:class:`~cuprum.adapters.tracing_adapter.TracingHook` needs from a tracing
library, and they are what an integrator implements. They live apart from the
hook so the contract can be read without the correlation machinery around it,
and so the hook module stays within the project's file-size ceiling.

:mod:`cuprum.adapters.tracing_adapter` re-exports both, so
``from cuprum.adapters.tracing_adapter import Span, Tracer`` — the import the
users' guide documents — keeps working.

Each method body raises :class:`NotImplementedError` rather than using an
ellipsis, matching :class:`~cuprum.adapters.metrics_adapter.MetricsCollector`,
the sibling adapter protocol. A docstring followed by ``...`` is what Pylint's
enabled ``unnecessary-ellipsis`` rule reports, and the project permits no
blanket suppression for it.
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
        raise NotImplementedError

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
        raise NotImplementedError

    def set_status(self, *, ok: bool) -> None:
        """Set the span status.

        Parameters
        ----------
        ok:
            True if the operation succeeded, False otherwise.

        """
        raise NotImplementedError

    def end(self) -> None:
        """End the span, recording its duration."""
        raise NotImplementedError


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
        raise NotImplementedError


__all__ = ["Span", "Tracer"]
