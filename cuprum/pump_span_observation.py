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

from cuprum.pump_span_events import (
    PUMP_HOP_OUTCOME_ATTRIBUTE,
    PUMP_HOP_SPAN_NAME,
    PUMP_HOP_TOTAL_BYTES_ATTRIBUTE,
    PumpHopOutcome,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from contextvars import Token

    from cuprum.adapters.tracing_protocols import Span, Tracer


_LOGGER = logging.getLogger(__name__)
_OUT_OF_ORDER_DETACH_MESSAGE = (
    "Pump-hop span registrations must be detached in last-in-first-out order"
)

_pump_span_tracers: ContextVar[tuple[Tracer, ...]] = ContextVar(
    "cuprum_pump_span_tracers",
    default=(),
)


class PumpHopSpanRegistration:
    """Registration handle for one Rust-pump hop tracer.

    The handle restores the precise preceding registration tuple when detached.
    It should therefore be detached in the :class:`~contextvars.Context` that
    created it and in last-in-first-out order when registrations nest.
    """

    __slots__ = ("_detached", "_registered", "_token", "_tracer")

    def __init__(self, tracer: Tracer) -> None:
        """Append ``tracer`` to the current context's hop tracers."""
        self._tracer = tracer
        self._detached = False
        self._registered = (*_pump_span_tracers.get(), tracer)
        self._token: Token[tuple[Tracer, ...]] = _pump_span_tracers.set(
            self._registered,
        )

    def detach(self) -> None:
        """Restore the tracers that preceded this registration."""
        if self._detached:
            return
        if _pump_span_tracers.get() is not self._registered:
            raise ValueError(_OUT_OF_ORDER_DETACH_MESSAGE)
        _pump_span_tracers.reset(self._token)
        self._detached = True

    def __enter__(self) -> typ.Self:
        """Enter the registration scope."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Detach the tracer on scope exit."""
        self.detach()


@dc.dataclass(frozen=True, slots=True)
class _PumpHopSpans:
    """Open spans owned by one executor future."""

    spans: tuple[Span, ...] = ()


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
        return _PumpHopSpans()

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
