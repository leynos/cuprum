"""Metrics contract tests for native-pump cancellation cleanup."""

from __future__ import annotations

from cuprum.adapters.pump_metrics import (
    RUST_PUMP_CLEANUP_DEFERRED_TOTAL,
    RUST_PUMP_CLEANUP_DURATION_SECONDS,
    RUST_PUMP_CLEANUP_GRACE_EXPIRED_TOTAL,
    RUST_PUMP_CLEANUP_TOTAL,
    PumpMetricsHook,
)
from cuprum.pump_events import PumpEvent
from cuprum.unittests._rust_pump_test_helpers import RecordingCollector


def test_a_completed_native_cleanup_records_count_and_duration() -> None:
    """A completed cleanup records one bounded count and one duration sample."""
    collector = RecordingCollector()

    PumpMetricsHook(collector)(PumpEvent(phase="cleanup_completed", duration_s=0.25))

    assert collector.counters == [(RUST_PUMP_CLEANUP_TOTAL, 1.0, {})], (
        "completed cleanup must increment its bounded counter, found "
        f"{collector.counters}"
    )
    assert collector.histograms == [(RUST_PUMP_CLEANUP_DURATION_SECONDS, 0.25, {})], (
        f"completed cleanup must observe its duration, found {collector.histograms}"
    )


def test_expired_and_deferred_cleanup_metrics_stay_unlabelled() -> None:
    """New cleanup outcomes add fixed-cardinality counters only."""
    collector = RecordingCollector()
    hook = PumpMetricsHook(collector)

    hook(PumpEvent(phase="cleanup_grace_expired", elapsed_s=0.25))
    hook(PumpEvent(phase="cleanup_deferred"))

    assert collector.counters == [
        (RUST_PUMP_CLEANUP_GRACE_EXPIRED_TOTAL, 1.0, {}),
        (RUST_PUMP_CLEANUP_DEFERRED_TOTAL, 1.0, {}),
    ], f"new cleanup outcomes must be unlabelled counters, found {collector.counters}"
    assert collector.histograms == [], (
        "grace expiry and deferred completion must not create a new histogram, "
        f"found {collector.histograms}"
    )
