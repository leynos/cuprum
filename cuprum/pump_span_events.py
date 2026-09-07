"""Stable names and outcomes for opt-in Rust-pump hop spans.

The executor-hop spans deliberately expose only bounded transfer facts. They
are independent of :mod:`cuprum.pump_events`, whose routing and cleanup events
remain a separate metrics-oriented observation channel.
"""

from __future__ import annotations

import enum


class PumpHopOutcome(enum.StrEnum):
    """The bounded terminal outcomes of one Rust-pump executor hop."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    FAILED_AFTER_CANCEL = "failed_after_cancel"


PUMP_HOP_SPAN_NAME = "cuprum.rust_pump_hop"
"""The tracing span name used for one Rust-pump executor hop."""

NATIVE_PUMP_BUFFER_SIZE = 65_536
"""The default byte buffer used by the native Rust pump."""

PUMP_HOP_OUTCOME_ATTRIBUTE = "cuprum.outcome"
PUMP_HOP_OPERATION_ATTRIBUTE = "cuprum.operation"
PUMP_HOP_BUFFER_SIZE_ATTRIBUTE = "cuprum.buffer_size"
PUMP_HOP_TOTAL_BYTES_ATTRIBUTE = "cuprum.total_bytes"

__all__ = [
    "NATIVE_PUMP_BUFFER_SIZE",
    "PUMP_HOP_BUFFER_SIZE_ATTRIBUTE",
    "PUMP_HOP_OPERATION_ATTRIBUTE",
    "PUMP_HOP_OUTCOME_ATTRIBUTE",
    "PUMP_HOP_SPAN_NAME",
    "PUMP_HOP_TOTAL_BYTES_ATTRIBUTE",
    "PumpHopOutcome",
]
