"""Reusable tracing assertions registered by the telemetry behaviour module."""

from __future__ import annotations

import typing as typ

import pytest

if typ.TYPE_CHECKING:
    from cuprum.adapters.tracing_memory import InMemoryTracer


def _require(*, condition: bool, message: str) -> None:
    """Fail a behaviour step when its required condition is false."""
    if not condition:
        pytest.fail(message)


def assert_span_attributes(behaviour_state: dict[str, object]) -> None:
    """Verify span attributes produced by a successful command."""
    tracer = typ.cast("InMemoryTracer", behaviour_state["tracer"])
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


def assert_span_events(behaviour_state: dict[str, object]) -> None:
    """Verify output is represented as tracing events."""
    tracer = typ.cast("InMemoryTracer", behaviour_state["tracer"])
    event_names = [name for name, _attributes in tracer.spans[0].events]
    _require(
        condition="cuprum.stdout" in event_names,
        message="Missing cuprum.stdout event",
    )
    _require(
        condition="cuprum.stderr" in event_names,
        message="Missing cuprum.stderr event",
    )


def assert_span_error_status(behaviour_state: dict[str, object]) -> None:
    """Verify a failing command ends its span with error status."""
    tracer = typ.cast("InMemoryTracer", behaviour_state["tracer"])
    span = tracer.spans[0]
    _require(
        condition=span.status_ok is False,
        message="Span status should indicate error",
    )
    _require(
        condition=span.attributes.get("cuprum.exit_code") == 1,
        message="Exit code should be 1",
    )
