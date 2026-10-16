"""Public shape and closed-category contracts for stream-echo events."""

from __future__ import annotations

import dataclasses as dc

from cuprum.echo_events import EchoErrorCategory, EchoEvent, EchoStream, RelayFallback


def test_delivery_categories_reuse_the_existing_event_shapes() -> None:
    """Delivery diagnostics add categories without exposing payload details."""
    assert EchoErrorCategory.DROPPED == "dropped"
    assert EchoErrorCategory.UNDELIVERED == "undelivered"
    assert EchoErrorCategory.DELIVERY_ERROR == "delivery_error"
    assert tuple(field.name for field in dc.fields(EchoEvent)) == (
        "stream",
        "error_category",
        "dropped_bytes",
    )
    assert tuple(field.name for field in dc.fields(RelayFallback)) == (
        "stream",
        "error_category",
    )

    event = EchoEvent(
        stream=EchoStream.STDOUT,
        error_category=EchoErrorCategory.DROPPED,
        dropped_bytes=7,
    )
    assert event.dropped_bytes == 7
