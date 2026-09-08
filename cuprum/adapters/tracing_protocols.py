"""Compatibility re-exports for the neutral tracing contracts.

New core and adapter code should import :class:`Span` and :class:`Tracer` from
:mod:`cuprum.tracing_protocols`. This module preserves the established adapter
import path without creating a second set of protocol classes.
"""

from cuprum.tracing_protocols import Span, Tracer

__all__ = ["Span", "Tracer"]
