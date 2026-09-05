"""Metric mapping tests for the closed Rust writer-resource outcome vocabulary."""

from __future__ import annotations

import typing as typ

import pytest

from cuprum.adapters.pump_metrics import RUST_PUMP_HANDOFF_TOTAL, PumpMetricsHook
from cuprum.pump_events import PumpEvent, RustPumpHandoffOutcome
from cuprum.unittests._rust_pump_test_helpers import RecordingCollector


@pytest.mark.parametrize("outcome", list(RustPumpHandoffOutcome))
def test_each_handoff_outcome_maps_to_one_fixed_metric(
    outcome: RustPumpHandoffOutcome,
) -> None:
    """Every production hand-off result increments one known labelled counter."""
    collector = RecordingCollector()

    PumpMetricsHook(collector)(PumpEvent(phase="handoff", outcome=outcome))

    assert collector.counters == [
        (RUST_PUMP_HANDOFF_TOTAL, 1.0, {"outcome": outcome})
    ], f"{outcome.value!r} must map to exactly one hand-off metric"
    assert collector.histograms == [], "hand-off outcomes must not create histograms"


def test_handoff_labels_are_limited_to_the_closed_outcome_vocabulary() -> None:
    """No descriptor, handle, error, or other unbounded value becomes a label."""
    collector = RecordingCollector()
    hook = PumpMetricsHook(collector)

    for outcome in RustPumpHandoffOutcome:
        hook(PumpEvent(phase="handoff", outcome=outcome))

    assert len(collector.counters) == len(RustPumpHandoffOutcome), (
        "each closed outcome must record exactly once"
    )
    for name, value, labels in collector.counters:
        assert name == RUST_PUMP_HANDOFF_TOTAL, (
            f"handoff outcomes must share the fixed metric name, found {name!r}"
        )
        assert value == 1.0, (  # ruff: ignore[float-equality-comparison] - counter increment is exact
            f"handoff outcome increment must be one, found {value}"
        )
        assert set(labels) == {"outcome"}, (
            f"handoff metric may carry only the outcome label, found {labels}"
        )
        assert labels["outcome"] in {item.value for item in RustPumpHandoffOutcome}, (
            f"handoff metric carried an unbounded outcome {labels['outcome']!r}"
        )


def test_invalid_handoff_outcome_emits_no_metric() -> None:
    """Malformed public events cannot introduce an unbounded outcome series."""
    collector = RecordingCollector()
    malformed = PumpEvent(
        phase="handoff",
        outcome=typ.cast("RustPumpHandoffOutcome", "descriptor-4732"),
    )

    PumpMetricsHook(collector)(malformed)

    assert collector.counters == [], (
        "an invalid outcome must not emit a submitted or arbitrary hand-off metric"
    )
