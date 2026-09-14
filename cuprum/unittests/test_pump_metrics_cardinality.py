"""Cardinality-boundary tests for pump metrics."""

from __future__ import annotations

import typing as typ

from cuprum.adapters.pump_metrics import (
    UNKNOWN_DECLINE_REASON,
    PumpMetricsHook,
)
from cuprum.pump_events import PumpEvent
from cuprum.unittests._rust_pump_test_helpers import RecordingCollector


class _ChattyReason:
    """A ``reason`` stand-in whose ``str()`` differs on every instance."""

    def __str__(self) -> str:
        """Return a value no two instances share."""
        return f"session-{id(self):x}"


def test_a_decline_carrying_a_non_enum_reason_is_still_bounded() -> None:
    """An off-enum ``reason`` degrades to the fixed label, not to ``str()``."""
    collector = RecordingCollector()
    reason = _ChattyReason()
    event = PumpEvent(phase="declined", reason=typ.cast("None", reason))

    PumpMetricsHook(collector)(event)

    _name, _value, labels = collector.counters[0]
    assert labels == {"reason": UNKNOWN_DECLINE_REASON}, (
        f"an unrecognized reason must degrade to {UNKNOWN_DECLINE_REASON!r} "
        f"rather than reaching the label, found {labels}"
    )
    assert str(reason) not in labels.values(), (
        "the object's own string must never appear as a label value, or the "
        f"series count follows the caller's data; found {labels}"
    )
