"""Reusable tracing assertions registered by the telemetry behaviour module."""

from __future__ import annotations

import typing as typ

import pytest
from pytest_bdd import then, when

from cuprum.adapters.tracing_memory import InMemoryTracer
from cuprum.pump_span_events import (
    PUMP_HOP_OUTCOME_ATTRIBUTE,
    PUMP_HOP_TOTAL_BYTES_ATTRIBUTE,
    PumpHopOutcome,
)
from tests.behaviour._rust_pump_span_support import (
    run_cancelled_hop,
    run_successful_hop,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class TracingBehaviourState(typ.TypedDict):
    """Behaviour state required by tracing assertions."""

    tracer: InMemoryTracer


_RUST_PUMP_EXPECTED_BYTES = 29


def tracing_state_from(
    behaviour_state: cabc.Mapping[str, object],
) -> TracingBehaviourState:
    """Validate and return the tracing state required by a later step."""
    tracer = behaviour_state.get("tracer")
    if not isinstance(tracer, InMemoryTracer):
        msg = "the tracing scenario has no in-memory tracer"
        raise TypeError(msg)
    return {"tracer": tracer}


def _pump_tracer_from(behaviour_state: cabc.Mapping[str, object]) -> InMemoryTracer:
    """Return the pump-hop tracer required by Rust-pump scenarios."""
    tracer = behaviour_state.get("pump_hop_tracer")
    if not isinstance(tracer, InMemoryTracer):
        msg = "the Rust-pump scenario has no in-memory tracer"
        raise TypeError(msg)
    return tracer


def _pump_hop_order_from(behaviour_state: cabc.Mapping[str, object]) -> list[str]:
    """Return the validated descriptor-restoration ordering."""
    order = behaviour_state.get("pump_hop_order")
    if not isinstance(order, list) or not all(
        isinstance(event, str) for event in order
    ):
        msg = "the Rust-pump scenario has no valid completion ordering"
        raise TypeError(msg)
    return order


def _require(*, condition: bool, message: str) -> None:
    """Fail a behaviour step when its required condition is false."""
    if not condition:
        pytest.fail(message)


def assert_span_attributes(behaviour_state: TracingBehaviourState) -> None:
    """Verify span attributes produced by a successful command."""
    tracer = behaviour_state["tracer"]
    span = tracer.spans[0]
    _require(
        condition="cuprum.program" in span.attributes,
        message="Missing cuprum.program attribute",
    )
    _require(
        condition="cuprum.exit_code" in span.attributes,
        message="Missing cuprum.exit_code attribute",
    )
    _require(
        condition=span.attributes["cuprum.exit_code"] == 0,
        message="Exit code should be 0",
    )


def assert_span_events(behaviour_state: TracingBehaviourState) -> None:
    """Verify output is represented as tracing events."""
    tracer = behaviour_state["tracer"]
    event_names = [name for name, _attributes in tracer.spans[0].events]
    _require(
        condition="cuprum.stdout" in event_names,
        message="Missing cuprum.stdout event",
    )
    _require(
        condition="cuprum.stderr" in event_names,
        message="Missing cuprum.stderr event",
    )


def assert_span_error_status(behaviour_state: TracingBehaviourState) -> None:
    """Verify a failing command ends its span with error status."""
    tracer = behaviour_state["tracer"]
    span = tracer.spans[0]
    _require(
        condition=span.status_ok is False,
        message="Span status should indicate error",
    )
    _require(
        condition=span.attributes.get("cuprum.exit_code") == 1,
        message="Exit code should be 1",
    )


@when("I run a successful Rust-pump executor hop")
def when_run_successful_rust_pump_hop(
    behaviour_state: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run a successful public pipeline through the Rust executor hop."""
    tracer = _pump_tracer_from(behaviour_state)
    run_successful_hop(tracer, monkeypatch)


@when("I cancel a Rust-pump executor hop while its worker owns descriptors")
def when_cancel_rust_pump_hop(
    behaviour_state: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel the executor task before its worker returns its descriptors."""
    tracer = _pump_tracer_from(behaviour_state)
    behaviour_state["pump_hop_order"] = run_cancelled_hop(tracer, monkeypatch)


@then("the pump-hop span is successful and records transferred bytes")
def then_successful_pump_hop_span(behaviour_state: dict[str, object]) -> None:
    """Assert the successful span's bounded terminal data."""
    tracer = _pump_tracer_from(behaviour_state)
    _require(
        condition=len(tracer.spans) == 1,
        message=f"expected one hop span, found {tracer.spans}",
    )
    span = tracer.spans[0]
    _require(condition=span.ended is True, message="successful hop span must end")
    _require(
        condition=span.status_ok is True,
        message="successful hop span must be marked ok",
    )
    _require(
        condition=span.attributes[PUMP_HOP_OUTCOME_ATTRIBUTE]
        is PumpHopOutcome.SUCCEEDED,
        message=f"unexpected hop outcome {span.attributes}",
    )
    _require(
        condition=span.attributes[PUMP_HOP_TOTAL_BYTES_ATTRIBUTE]
        == _RUST_PUMP_EXPECTED_BYTES,
        message=f"unexpected transferred-byte count {span.attributes}",
    )


@then("the pump-hop span is cancelled after the worker returns")
def then_cancelled_pump_hop_span(behaviour_state: dict[str, object]) -> None:
    """Assert cancellation spans include the worker-owned cleanup window."""
    tracer = _pump_tracer_from(behaviour_state)
    order = _pump_hop_order_from(behaviour_state)
    _require(
        condition=len(tracer.spans) == 1,
        message=f"expected one hop span, found {tracer.spans}",
    )
    span = tracer.spans[0]
    _require(condition=span.ended is True, message="cancelled hop span must end")
    _require(
        condition=span.attributes[PUMP_HOP_OUTCOME_ATTRIBUTE]
        is PumpHopOutcome.CANCELLED,
        message=f"unexpected hop outcome {span.attributes}",
    )
    _require(
        condition=span.status_ok is None,
        message="only successful spans may be marked ok",
    )
    _require(
        condition=order.index("worker_returned") < order.index("restored"),
        message=f"worker must return before descriptors restore: {order}",
    )
