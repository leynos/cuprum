"""Unit tests for the opt-in Rust-pump executor-hop span registry."""

from __future__ import annotations

import logging
import typing as typ

import pytest

from cuprum.adapters.tracing_memory import InMemoryTracer
from cuprum.pump_span_events import PUMP_HOP_SPAN_NAME
from cuprum.pump_span_observation import (
    _close_pump_hop_spans,
    _open_pump_hop_spans,
    current_pump_span_tracers,
    observe_pump_span,
)

if typ.TYPE_CHECKING:
    from cuprum.adapters.tracing_protocols import Tracer


class _FailingTracer:
    """Tracer double whose span creation fails normally."""

    def start_span(self, _name: str, _attributes: object = None) -> object:
        """Raise the observer failure being contained."""
        msg = "tracer backend unavailable"
        raise RuntimeError(msg)


class _InterruptingTracer:
    """Tracer double whose shutdown signal must propagate."""

    def start_span(self, _name: str, _attributes: object = None) -> object:
        """Raise a non-Exception control-flow signal."""
        raise KeyboardInterrupt


def test_registration_restores_the_prior_tracer_tuple() -> None:
    """Registrations support nested context-manager token restoration."""
    outer = InMemoryTracer()
    inner = InMemoryTracer()

    assert current_pump_span_tracers() == (), "the registry must begin empty"
    with observe_pump_span(outer):
        assert current_pump_span_tracers() == (outer,), "outer tracer is missing"
        with observe_pump_span(inner):
            assert current_pump_span_tracers() == (outer, inner), (
                "nested registration must retain both tracers"
            )
        assert current_pump_span_tracers() == (outer,), (
            "inner detachment must restore only the outer tracer"
        )
    assert current_pump_span_tracers() == (), "scope exit must restore emptiness"


def test_no_registration_opens_no_spans() -> None:
    """The unregistered channel returns an empty carrier immediately."""
    spans = _open_pump_hop_spans({"cuprum.operation": "rust_pump"})

    assert spans.spans == (), "an unregistered channel must not open a span"


def test_failing_tracer_is_reported_and_other_tracers_continue(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ``Exception`` from one tracer cannot abort another observer."""
    working = InMemoryTracer()
    with (
        caplog.at_level(logging.WARNING, logger="cuprum.pump_span_observation"),
        observe_pump_span(typ.cast("Tracer", _FailingTracer())),
        observe_pump_span(working),
    ):
        spans = _open_pump_hop_spans({"cuprum.operation": "rust_pump"})
        _close_pump_hop_spans(spans, outcome="succeeded", total_bytes=7)

    assert len(working.spans) == 1, "the later tracer must still open its span"
    assert working.spans[0].name == PUMP_HOP_SPAN_NAME, "span name mismatch"
    assert working.spans[0].ended is True, "the working span must be ended"
    records = [
        record
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "pump_span_observer_failed"
    ]
    assert len(records) == 1, f"expected one contained failure, found {records}"


def test_non_exception_from_tracer_propagates() -> None:
    """Shutdown control flow must not be absorbed as an observer failure."""
    with (
        observe_pump_span(
            typ.cast("Tracer", _InterruptingTracer()),
        ),
        pytest.raises(KeyboardInterrupt),
    ):
        _open_pump_hop_spans({"cuprum.operation": "rust_pump"})
