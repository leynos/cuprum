"""Stateful contracts for Rust-pump hop-span tracer registrations.

``PumpHopSpanRegistration`` stores its registrations in a context-local tuple
and requires nested handles to detach in last-in-first-out order. This machine
uses a fresh :class:`contextvars.Context` per example and maintains a separate
tuple-and-handle-stack model to exercise registration, normal cleanup, repeated
cleanup, context-manager exits, and rejected out-of-order cleanup.
"""

from __future__ import annotations

import contextvars
import typing as typ

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from cuprum.adapters.tracing_memory import InMemoryTracer
from cuprum.pump_span_observation import (
    PumpHopSpanRegistration,
    current_pump_span_tracers,
    observe_pump_span,
)

if typ.TYPE_CHECKING:
    from hypothesis.strategies import DataObject

    from cuprum.tracing_protocols import Tracer


type _ActiveRegistration = tuple[
    PumpHopSpanRegistration,
    tuple[Tracer, ...],
    tuple[Tracer, ...],
]


class PumpHopSpanRegistrationMachine(RuleBasedStateMachine):
    """Drive context-local tracer registrations against an independent model."""

    def __init__(self) -> None:
        """Create one isolated context with no registered tracer."""
        super().__init__()
        self._context = contextvars.Context()
        self._baseline = self._context.run(current_pump_span_tracers)
        self._expected: tuple[Tracer, ...] = self._baseline
        self._active: list[_ActiveRegistration] = []
        self._detached: list[PumpHopSpanRegistration] = []

    def _actual(self) -> tuple[Tracer, ...]:
        """Read the current context-local tracer tuple."""
        return self._context.run(current_pump_span_tracers)

    def _assert_model(self) -> None:
        """Assert that the implementation's tuple is the modelled tuple."""
        actual = self._actual()
        assert actual == self._expected, (
            "registered tracers must equal the independent tuple model"
        )

    @rule()
    def register(self) -> None:
        """Append one tracer and record the tuple preceding its handle."""
        tracer = InMemoryTracer()
        prior = self._expected
        handle = self._context.run(observe_pump_span, tracer)
        registered = self._actual()

        assert registered == (*prior, tracer), (
            "registration must append its tracer to the preceding tuple"
        )
        self._active.append((handle, prior, registered))
        self._expected = registered

    @precondition(lambda self: bool(self._active))
    @rule()
    def detach_innermost(self) -> None:
        """Detach the newest handle and restore its exact preceding tuple."""
        handle, prior, _registered = self._active.pop()
        self._context.run(handle.detach)
        self._expected = prior
        self._detached.append(handle)
        self._assert_model()

    @precondition(lambda self: bool(self._detached))
    @rule(data=st.data())
    def repeat_detach(self, data: DataObject) -> None:
        """Repeat detachment of an inactive handle without changing the tuple."""
        handle = data.draw(st.sampled_from(self._detached))
        before = self._actual()
        self._context.run(handle.detach)

        assert self._actual() is before, (
            "repeated detachment must leave the exact tuple object unchanged"
        )
        self._assert_model()

    @rule()
    def context_manager_exit(self) -> None:
        """Exit a scoped registration and retain the original tuple."""
        tracer = InMemoryTracer()
        prior = self._expected

        def enter_and_exit() -> PumpHopSpanRegistration:
            """Register then exit in the isolated context."""
            with observe_pump_span(tracer) as handle:
                assert current_pump_span_tracers() == (*prior, tracer), (
                    "context manager entry must append its tracer"
                )
            return handle

        handle = self._context.run(enter_and_exit)
        self._detached.append(handle)
        assert self._actual() is prior, (
            "context manager exit must restore the exact preceding tuple"
        )
        self._assert_model()

    @precondition(lambda self: len(self._active) >= 2)
    @rule()
    def out_of_order_detach(self) -> None:
        """Reject detaching an outer handle while an inner one remains active."""
        outer, _prior, _registered = self._active[-2]
        before = self._actual()

        with pytest.raises(ValueError, match="last-in-first-out"):
            self._context.run(outer.detach)

        assert self._actual() is before, (
            "an out-of-order rejection must leave the registered tuple unchanged"
        )
        self._assert_model()

    @invariant()
    def registered_tuple_matches_the_model(self) -> None:
        """Check the context-local tuple after every generated operation."""
        self._assert_model()

    def teardown(self) -> None:
        """Detach remaining handles in LIFO order and restore the empty baseline."""
        while self._active:
            handle, prior, _registered = self._active.pop()
            self._context.run(handle.detach)
            self._expected = prior
            self._detached.append(handle)
        self._assert_model()
        assert self._actual() is self._baseline, (
            "state-machine cleanup must restore the exact empty baseline tuple"
        )


TestPumpHopSpanRegistrationMachine = PumpHopSpanRegistrationMachine.TestCase
TestPumpHopSpanRegistrationMachine.settings = settings(
    max_examples=40,
    stateful_step_count=20,
    deadline=None,
)
