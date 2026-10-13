"""Unit tests for the bounded metrics projected from control events.

The label set is what these tests exist to pin. A metric label taken from an
open field is an unbounded series, so the projection has to stay closed even
when the event it is handed was built by hand with a value outside the enums
rather than emitted by a handle.
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum.adapters.control_metrics import (
    CONTROL_OPERATION_DURATION_SECONDS,
    CONTROL_OPERATIONS_TOTAL,
    CONTROL_REFUSALS_TOTAL,
    ControlMetricsHook,
    control_metrics_hook,
)
from cuprum.control_events import (
    ControlEvent,
    ControlOperation,
    ControlOutcome,
)
from cuprum.unittests._rust_pump_test_helpers import RecordingCollector

if typ.TYPE_CHECKING:
    from cuprum.adapters.metrics_adapter import MetricsCollector


def _event(
    operation: ControlOperation = ControlOperation.TERMINATE,
    outcome: ControlOutcome = ControlOutcome.DELIVERED,
    *,
    duration_s: float = 0.25,
    pid: int | None = 4321,
) -> ControlEvent:
    """Build a completed control event with a bounded target by default."""
    return ControlEvent(
        operation=operation,
        outcome=outcome,
        duration_s=duration_s,
        pid=pid,
    )


def _rejected_event(operation: object, outcome: object) -> ControlEvent:
    """Build an event whose closed fields hold values outside their enums."""
    return ControlEvent(
        operation=typ.cast("ControlOperation", operation),
        outcome=typ.cast("ControlOutcome", outcome),
        duration_s=0.25,
    )


class TestProjection:
    """Turning one control event into counters and a duration observation."""

    def test_records_a_counter_and_a_duration_per_event(self) -> None:
        """Emit exactly one counter and one histogram observation per event."""
        collector = RecordingCollector()

        ControlMetricsHook(collector)(_event(duration_s=0.5))

        assert [name for name, _v, _l in collector.counters] == [
            CONTROL_OPERATIONS_TOTAL
        ], "one counter"
        assert collector.histograms == [
            (
                CONTROL_OPERATION_DURATION_SECONDS,
                0.5,
                {
                    "operation": "terminate",
                    "outcome": "delivered",
                },
            )
        ], "one duration observation"

    @pytest.mark.parametrize(
        ("operation", "outcome"),
        [
            (ControlOperation.SIGNAL, ControlOutcome.DELIVERED),
            (ControlOperation.TERMINATE, ControlOutcome.ESCALATED),
            (ControlOperation.KILL, ControlOutcome.REFUSED_EXITED),
            (ControlOperation.KILL, ControlOutcome.REFUSED_UNSUPPORTED),
        ],
        ids=str,
    )
    def test_labels_carry_only_the_closed_operation_and_outcome(
        self,
        operation: ControlOperation,
        outcome: ControlOutcome,
    ) -> None:
        """Label every metric with exactly the two closed field values."""
        collector = RecordingCollector()

        ControlMetricsHook(collector)(_event(operation, outcome))

        (_name, _value, labels) = collector.counters[0]
        assert labels["operation"] == str(operation), "the operation value"
        assert labels["outcome"] == str(outcome), "the outcome value"
        assert set(labels) == {"operation", "outcome"}, "no other label is added"

    def test_a_pid_never_reaches_a_label(self) -> None:
        """Keep the unbounded process identifier off every metric."""
        collector = RecordingCollector()

        ControlMetricsHook(collector)(_event(pid=987654))

        assert "987654" not in repr(collector.counters), "no PID in a counter"
        assert "987654" not in repr(collector.histograms), "no PID in a histogram"

    def test_counts_only_the_refusals_in_the_refusal_counter(self) -> None:
        """Increment the refusal counter for the two refusing outcomes only."""
        collector = RecordingCollector()
        hook = ControlMetricsHook(collector)

        hook(_event(outcome=ControlOutcome.REFUSED_EXITED))
        hook(_event(outcome=ControlOutcome.REFUSED_UNSUPPORTED))
        hook(_event(outcome=ControlOutcome.ESCALATED))

        refusals = [
            (name, labels)
            for name, _value, labels in collector.counters
            if name == CONTROL_REFUSALS_TOTAL
        ]
        assert refusals == [
            (CONTROL_REFUSALS_TOTAL, {"operation": "terminate"}),
            (CONTROL_REFUSALS_TOTAL, {"operation": "terminate"}),
        ], "two refusals, each labelled by operation alone"

    def test_the_refusal_counter_learns_nothing_from_the_outcome(self) -> None:
        """Leave the outcome off the refusal counter, which already means it."""
        collector = RecordingCollector()

        ControlMetricsHook(collector)(_event(outcome=ControlOutcome.REFUSED_EXITED))

        refusals = [
            labels
            for name, _value, labels in collector.counters
            if name == CONTROL_REFUSALS_TOTAL
        ]
        assert refusals == [{"operation": "terminate"}], (
            "the refusal counter is keyed on the operation alone"
        )


class TestBoundedness:
    """Refusing to widen the label set for an event nobody emitted."""

    @pytest.mark.parametrize(
        ("operation", "outcome"),
        [
            ("pend", ControlOutcome.DELIVERED),
            (ControlOperation.SIGNAL, "vanished"),
            ("pend", "vanished"),
        ],
        ids=("open-operation", "open-outcome", "both-open"),
    )
    def test_ignores_an_event_outside_the_closed_enums(
        self,
        operation: object,
        outcome: object,
    ) -> None:
        """Record nothing for an event whose fields are not closed members."""
        collector = RecordingCollector()

        ControlMetricsHook(collector)(_rejected_event(operation, outcome))

        assert collector.counters == [], "no counter is recorded"
        assert collector.histograms == [], "no duration is recorded"

    def test_an_accepted_event_still_records_after_a_rejected_one(self) -> None:
        """Keep projecting valid events once an unusable one is skipped."""
        collector = RecordingCollector()
        hook = ControlMetricsHook(collector)

        hook(_rejected_event("pend", "vanished"))
        hook(_event())

        assert len(collector.counters) == 1, "the valid event still counts"
        assert collector.counters[0][2] == {
            "operation": "terminate",
            "outcome": "delivered",
        }, "the valid event's labels"


def test_the_factory_returns_the_hook_protocol() -> None:
    """Return a usable control hook from the collector factory."""
    collector = RecordingCollector()
    hook = control_metrics_hook(typ.cast("MetricsCollector", collector))

    hook(_event())

    assert collector.counter_names() == [CONTROL_OPERATIONS_TOTAL], "the factory's hook"
