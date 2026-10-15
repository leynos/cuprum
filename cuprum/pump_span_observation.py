"""Opt-in tracing spans around Rust-pump executor hops.

Registrations live on their own :class:`~contextvars.ContextVar`, so adding a
tracer does not alter command contexts, pump events, or existing hooks. The
executor callback owns closure because its future settles only after the Rust
worker has released its duplicate descriptor.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import typing as typ
from contextvars import ContextVar

from cuprum._scope_registration import _TokenTupleRegistration
from cuprum.pump_span_events import (
    PUMP_HOP_OUTCOME_ATTRIBUTE,
    PUMP_HOP_SPAN_NAME,
    PUMP_HOP_TOTAL_BYTES_ATTRIBUTE,
    PumpHopOutcome,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.tracing_protocols import Span, Tracer


_LOGGER = logging.getLogger(__name__)
_OUT_OF_ORDER_DETACH_MESSAGE = (
    "Pump-hop span registrations must be detached in last-in-first-out order"
)

_pump_span_tracers: ContextVar[tuple[Tracer, ...]] = ContextVar(
    "cuprum_pump_span_tracers",
    default=(),
)


class PumpHopSpanRegistration(_TokenTupleRegistration["Tracer"]):
    """Registration handle for one Rust-pump hop tracer.

    The handle restores the precise preceding registration tuple when detached.
    It should therefore be detached in the :class:`~contextvars.Context` that
    created it and in last-in-first-out order when registrations nest.

    Unlike the other registrations built on
    :class:`~cuprum._scope_registration._TokenTupleRegistration`, this one
    refuses an out-of-order detach rather than quietly discarding whatever a
    later registration added: it keeps the tuple it installed so
    :meth:`_release` can compare it against the live one first.
    """

    __slots__ = ("_registered",)

    def __init__(self, tracer: Tracer) -> None:
        """Append ``tracer`` to the current context's hop tracers."""
        super().__init__(_pump_span_tracers, tracer)

    def _append(self) -> None:
        """Install through the base, then keep the tuple that install created.

        The base's install already sets the extended tuple and captures the
        restore token, so the only thing left to record is the tuple itself:
        :meth:`_release` compares it against the live one to refuse an
        out-of-order detach. Re-deriving the install here instead, as this
        handle once did from ``__init__``, skips the base initializer and
        leaves the inherited hook state unset.
        """
        super()._append()
        self._registered: tuple[Tracer, ...] = self._hook_var.get()

    def _release(self) -> None:
        """Refuse an out-of-order detach, then restore the prior tracers."""
        if self._hook_var.get() is not self._registered:
            raise ValueError(_OUT_OF_ORDER_DETACH_MESSAGE)
        super()._release()


@dc.dataclass(frozen=True, slots=True)
class _PumpHopSpans:
    """Open spans owned by one executor future."""

    spans: tuple[Span, ...] = ()


_EMPTY_PUMP_HOP_SPANS = _PumpHopSpans()


def current_pump_span_tracers() -> tuple[Tracer, ...]:
    """Return the hop tracers registered in the current context."""
    return _pump_span_tracers.get()


def observe_pump_span(tracer: Tracer) -> PumpHopSpanRegistration:
    """Register a tracer for Rust-pump executor-hop spans.

    Parameters
    ----------
    tracer:
        Backend that opens a span for every Rust-pump hop started in this
        context.

    Returns
    -------
    PumpHopSpanRegistration
        A handle that can detach the tracer or scope it with a ``with`` block.
    """
    return PumpHopSpanRegistration(tracer)


def _open_pump_hop_spans(attributes: cabc.Mapping[str, object]) -> _PumpHopSpans:
    """Open one Rust-pump hop span for each registered tracer."""
    tracers = _pump_span_tracers.get()
    if not tracers:
        return _EMPTY_PUMP_HOP_SPANS

    spans: list[Span] = []
    for tracer in tracers:
        try:
            spans.append(tracer.start_span(PUMP_HOP_SPAN_NAME, attributes))
        except Exception as exc:
            _LOGGER.exception(
                "pump_span_observer_failed",
                extra=_observer_failure_extra(exc),
            )
        except BaseException:
            _close_pump_hop_spans(
                _PumpHopSpans(tuple(spans)),
                outcome=PumpHopOutcome.FAILED,
                total_bytes=None,
            )
            raise
    return _PumpHopSpans(tuple(spans))


def _close_pump_hop_spans(
    spans: _PumpHopSpans,
    *,
    outcome: PumpHopOutcome,
    total_bytes: int | None,
) -> None:
    """Record one terminal outcome and close every open hop span."""
    for span in spans.spans:
        try:
            try:
                span.set_attribute(PUMP_HOP_OUTCOME_ATTRIBUTE, outcome)
                if outcome is PumpHopOutcome.SUCCEEDED:
                    if total_bytes is not None:
                        span.set_attribute(PUMP_HOP_TOTAL_BYTES_ATTRIBUTE, total_bytes)
                    span.set_status(ok=True)
            except Exception as exc:
                _LOGGER.exception(
                    "pump_span_observer_failed",
                    extra=_observer_failure_extra(exc),
                )
        finally:
            try:
                span.end()
            except Exception as exc:
                _LOGGER.exception(
                    "pump_span_observer_failed",
                    extra=_observer_failure_extra(exc),
                )


def _observer_failure_extra(exc: Exception) -> dict[str, str]:
    """Return bounded structured context for an observer failure."""
    return {
        "cuprum_action": "pump_span_observer_failed",
        "cuprum_error_type": type(exc).__name__,
    }


__all__ = [
    "PumpHopSpanRegistration",
    "current_pump_span_tracers",
    "observe_pump_span",
]
