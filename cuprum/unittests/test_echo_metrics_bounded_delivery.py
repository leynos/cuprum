"""Metrics coverage for bounded-delivery echo events."""

from __future__ import annotations

import pytest

from cuprum.adapters.echo_metrics import (
    ECHO_DELIVERY_BYTES_TOTAL,
    ECHO_DELIVERY_EVENTS_TOTAL,
    EchoMetricsHook,
)
from cuprum.echo_events import EchoErrorCategory, EchoEvent, EchoStream
from cuprum.unittests._rust_pump_test_helpers import RecordingCollector


@pytest.mark.parametrize(
    ("category", "byte_count"),
    [
        (EchoErrorCategory.DROPPED, 7),
        (EchoErrorCategory.DELIVERY_ERROR, None),
        (EchoErrorCategory.UNDELIVERED, 11),
    ],
    ids=["queue-drop", "sink-error", "shutdown-loss"],
)
def test_delivery_events_and_bytes_use_bounded_metric_labels(
    category: EchoErrorCategory,
    byte_count: int | None,
) -> None:
    """Every delivery outcome is counted without payload or sink labels."""
    collector = RecordingCollector()
    hook = EchoMetricsHook(collector)
    event = EchoEvent(
        stream=EchoStream.STDERR,
        error_category=category,
        dropped_bytes=byte_count,
    )

    hook(event)

    event_name, event_count, event_labels = collector.counters[0]
    assert event_name == ECHO_DELIVERY_EVENTS_TOTAL
    assert event_count == pytest.approx(1.0)
    assert event_labels == {"stream": "stderr", "error_category": category.value}
    if byte_count is None:
        assert len(collector.counters) == 1
    else:
        byte_metric, byte_count_value, byte_labels = collector.counters[1]
        assert byte_metric == ECHO_DELIVERY_BYTES_TOTAL
        assert byte_count_value == pytest.approx(float(byte_count))
        assert byte_labels == {
            "stream": "stderr",
            "error_category": category.value,
        }
