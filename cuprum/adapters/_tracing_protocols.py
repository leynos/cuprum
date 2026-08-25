"""Compatibility re-export for tracing ``Span`` and ``Tracer`` protocols.

The canonical definitions live in :mod:`cuprum.adapters.tracing_protocols`.
This private module preserves legacy imports without creating another protocol
contract.
"""

from cuprum.adapters.tracing_protocols import Span, Tracer

__all__ = ["Span", "Tracer"]
