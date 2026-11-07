"""The held-pair gauge reported by ``PumpMetricsHook``.

The gauge answers a question the counters cannot: how many descriptor pairs a
hand-off is holding while its buffered drain is still pending. These tests pin
the bounded accumulation, the optional-collector contract, and the balance
across concurrent drains.
"""

from __future__ import annotations

import threading
import typing as typ

import pytest

from cuprum.adapters.gauge_metrics import GaugeMetricsCollector
from cuprum.adapters.metrics_adapter import InMemoryMetrics
from cuprum.adapters.pump_metrics import (
    RUST_PUMP_DECLINED_TOTAL,
    RUST_PUMP_HELD_PAIRS,
    PumpMetricsHook,
)
from cuprum.pump_events import PumpEvent, RustPumpDeclineReason
from cuprum.unittests._rust_pump_test_helpers import RecordingCollector

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _hold(delta: int) -> PumpEvent:
    """Build the hold transition the coordinator emits."""
    return PumpEvent(phase="handoff_hold", held_delta=delta)


def test_a_hold_raises_and_returns_the_gauge() -> None:
    """One acquisition and one release leave the gauge at zero."""
    collector = RecordingCollector()
    hook = PumpMetricsHook(collector)

    hook(_hold(1))
    hook(_hold(-1))

    assert collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 0.0], (
        "the gauge must report the held pair and its release"
    )


class _TwoMethodCollector:
    """A collector written before gauges existed, with no ``set_gauge``."""

    def __init__(self) -> None:
        """Start with no recorded counters."""
        self.counters: list[str] = []

    def inc_counter(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record the counter name."""
        del value, labels
        self.counters.append(name)

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Ignore histogram observations."""
        del name, value, labels


def test_a_collector_without_gauges_still_receives_counters() -> None:
    """Gauge support is optional, so an unchanged collector keeps working."""
    collector = _TwoMethodCollector()
    hook = PumpMetricsHook(typ.cast("typ.Any", collector))

    hook(_hold(1))
    hook(_hold(-1))
    hook(PumpEvent(phase="declined", reason=RustPumpDeclineReason.RAW_FD_UNAVAILABLE))

    assert collector.counters == [RUST_PUMP_DECLINED_TOTAL], (
        "a collector without a gauge must still receive the counters it supports"
    )


@pytest.mark.parametrize("delta", [0, 2, -2], ids=["zero", "double", "negative"])
def test_a_malformed_hold_delta_is_ignored(delta: int) -> None:
    """A hand-built event cannot move the gauge by an unbounded amount."""
    collector = RecordingCollector()

    PumpMetricsHook(collector)(_hold(delta))

    assert not collector.gauges, (
        f"only a single acquisition or release may move the gauge, found {delta}"
    )


def test_concurrent_drains_accumulate_under_one_hook() -> None:
    """Four drains held at once are reported as four held pairs."""
    collector = RecordingCollector()
    hook = PumpMetricsHook(collector)
    held_by_all = threading.Barrier(4)

    def hold_and_release() -> None:
        """Hold a pair until every drain holds one, then release it."""
        hook(_hold(1))
        held_by_all.wait(timeout=5.0)
        hook(_hold(-1))

    workers = [threading.Thread(target=hold_and_release) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5.0)

    values = collector.gauge_values(RUST_PUMP_HELD_PAIRS)
    assert max(values) == pytest.approx(4.0), (
        f"every concurrently held pair must be counted, found {values}"
    )
    assert values[-1] == pytest.approx(0.0), (
        f"the gauge must return to zero, found {values}"
    )
    assert all(0.0 <= value <= 4.0 for value in values), (
        f"the gauge must stay bounded by the drains in flight, found {values}"
    )


def test_the_reference_collector_reports_and_clears_gauges() -> None:
    """``InMemoryMetrics`` provides the optional capability."""
    metrics = InMemoryMetrics()

    assert isinstance(metrics, GaugeMetricsCollector), (
        "the reference collector must offer the gauge capability"
    )
    PumpMetricsHook(metrics)(_hold(1))
    assert metrics.gauges[RUST_PUMP_HELD_PAIRS] == pytest.approx(1.0)

    metrics.reset()
    assert not metrics.gauges, "reset must clear reported gauges"
