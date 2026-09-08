"""Unit tests for the opt-in Rust-pump executor-hop span registry."""

from __future__ import annotations

import logging
import typing as typ
from pathlib import Path

import pytest

from cuprum import pump_span_observation
from cuprum.adapters import tracing_protocols as adapter_tracing_protocols
from cuprum.adapters.tracing_adapter import (
    Span as AdapterSpan,
)
from cuprum.adapters.tracing_adapter import (
    Tracer as AdapterTracer,
)
from cuprum.adapters.tracing_memory import InMemoryTracer
from cuprum.pump_span_events import PUMP_HOP_SPAN_NAME, PumpHopOutcome
from cuprum.pump_span_observation import (
    _close_pump_hop_spans,
    _open_pump_hop_spans,
    current_pump_span_tracers,
    observe_pump_span,
)
from cuprum.tracing_protocols import Span, Tracer


class _FailingTracer:
    """Tracer double whose span creation fails normally."""

    def start_span(self, _name: str, _attributes: object = None) -> object:
        """Raise the observer failure being contained."""
        msg = "tracer backend unavailable"
        raise OSError(msg)


class _InterruptingTracer:
    """Tracer double whose shutdown signal must propagate."""

    def start_span(self, _name: str, _attributes: object = None) -> object:
        """Raise a non-Exception control-flow signal."""
        raise KeyboardInterrupt


class TestPumpSpanObservation:
    """Registry and observer-failure contracts for pump-hop spans."""

    def test_tracing_protocol_imports_preserve_contract_identity(self) -> None:
        """Core and legacy adapter paths expose the same protocol objects."""
        assert adapter_tracing_protocols.Span is Span
        assert adapter_tracing_protocols.Tracer is Tracer
        assert AdapterSpan is Span
        assert AdapterTracer is Tracer

    def test_core_pump_observation_has_no_adapter_protocol_dependency(self) -> None:
        """The core observation boundary imports its contracts from core."""
        module_source = pump_span_observation.__file__
        assert module_source is not None
        source = Path(module_source).read_text(encoding="utf-8")

        assert "cuprum.adapters.tracing_protocols" not in source
        assert "from cuprum.tracing_protocols import Span, Tracer" in source

    def test_registration_restores_the_prior_tracer_tuple(self) -> None:
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

    def test_outer_detach_before_inner_is_rejected(self) -> None:
        """A registration cannot reset a newer context-local tuple."""
        outer = observe_pump_span(InMemoryTracer())
        inner = observe_pump_span(InMemoryTracer())

        with pytest.raises(ValueError, match="last-in-first-out"):
            outer.detach()
        assert len(current_pump_span_tracers()) == 2
        inner.detach()
        outer.detach()
        outer.detach()
        assert current_pump_span_tracers() == ()

    def test_no_registration_opens_no_spans(self) -> None:
        """The unregistered channel returns an empty carrier immediately."""
        spans = _open_pump_hop_spans({"cuprum.operation": "rust_pump"})

        assert spans.spans == (), "an unregistered channel must not open a span"

    def test_failing_tracer_is_reported_and_other_tracers_continue(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An ``Exception`` from one tracer cannot abort another observer."""
        working = InMemoryTracer()
        with (
            caplog.at_level(logging.ERROR, logger="cuprum.pump_span_observation"),
            observe_pump_span(typ.cast("Tracer", _FailingTracer())),
            observe_pump_span(working),
        ):
            spans = _open_pump_hop_spans({"cuprum.operation": "rust_pump"})
            _close_pump_hop_spans(
                spans,
                outcome=PumpHopOutcome.SUCCEEDED,
                total_bytes=7,
            )

        assert len(working.spans) == 1, "the later tracer must still open its span"
        assert working.spans[0].name == PUMP_HOP_SPAN_NAME, "span name mismatch"
        assert working.spans[0].ended is True, "the working span must be ended"
        records = [
            record
            for record in caplog.records
            if record.__dict__.get("cuprum_action") == "pump_span_observer_failed"
        ]
        assert len(records) == 1, f"expected one contained failure, found {records}"

    def test_non_exception_from_tracer_closes_prior_spans(self) -> None:
        """Control flow closes already-open spans before it propagates."""
        working = InMemoryTracer()
        with (
            observe_pump_span(working),
            observe_pump_span(typ.cast("Tracer", _InterruptingTracer())),
            pytest.raises(KeyboardInterrupt),
        ):
            _open_pump_hop_spans({"cuprum.operation": "rust_pump"})

        assert working.spans[0].ended is True, "partial span must be closed"
        assert working.spans[0].attributes["cuprum.outcome"] is PumpHopOutcome.FAILED

    def test_non_exception_from_tracer_propagates(self) -> None:
        """Shutdown control flow must not be absorbed as an observer failure."""
        with (
            observe_pump_span(typ.cast("Tracer", _InterruptingTracer())),
            pytest.raises(KeyboardInterrupt),
        ):
            _open_pump_hop_spans({"cuprum.operation": "rust_pump"})
